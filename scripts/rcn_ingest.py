#!/usr/bin/env python3
"""
RCN (Rejestr Cen Nieruchomości) ingestion script.

Downloads transactional real-estate data from GUGiK's public WFS service
(https://mapy.geoportal.gov.pl/wss/service/rcn) for top Polish cities and stores
them into MongoDB collection `rcn_transactions`.

Data source is free since 13 Feb 2026 (Act of 26 Sep 2025).
Attributes returned by RCN (per §40 EGiB regulation):
- ceny_brutto (PLN)
- powierzchnia (m²) — for lokale
- rodzaj_nieruchomosci (lokal / budynek / dzialka)
- rodzaj_rynku (pierwotny / wtórny)
- data_zawarcia (YYYY-MM-DD)
- teryt (kod powiatu)

Usage:
    export MONGO_URL="mongodb+srv://..."
    python scripts/rcn_ingest.py                    # all 20 cities
    python scripts/rcn_ingest.py --city Warszawa    # single city
    python scripts/rcn_ingest.py --since 2024-01-01 # only transactions since date

The script is idempotent — uses a compound unique index on
(teryt, data_zawarcia, cena_brutto, powierzchnia) to skip duplicates.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError, BulkWriteError

# ------------------------------------------------------------------ config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rcn")

WFS_URL = "https://mapy.geoportal.gov.pl/wss/service/rcn"
WFS_TIMEOUT = 90  # seconds

# Bounding boxes (min_lon, min_lat, max_lon, max_lat) for top 20 Polish cities.
# EPSG:4326. Boxes are slightly padded to include suburbs.
CITIES: Dict[str, Dict[str, Any]] = {
    "Warszawa":     {"bbox": (20.85, 52.10, 21.30, 52.38), "teryt": "1465"},
    "Kraków":       {"bbox": (19.80, 49.98, 20.16, 50.14), "teryt": "1261"},
    "Wrocław":      {"bbox": (16.90, 51.02, 17.20, 51.20), "teryt": "0264"},
    "Łódź":         {"bbox": (19.36, 51.68, 19.60, 51.85), "teryt": "1061"},
    "Poznań":       {"bbox": (16.80, 52.30, 17.10, 52.50), "teryt": "3064"},
    "Gdańsk":       {"bbox": (18.45, 54.30, 18.80, 54.45), "teryt": "2261"},
    "Szczecin":     {"bbox": (14.40, 53.35, 14.75, 53.55), "teryt": "3262"},
    "Bydgoszcz":    {"bbox": (17.90, 53.05, 18.15, 53.20), "teryt": "0461"},
    "Lublin":       {"bbox": (22.45, 51.18, 22.70, 51.30), "teryt": "0663"},
    "Białystok":    {"bbox": (23.05, 53.05, 23.30, 53.20), "teryt": "2061"},
    "Katowice":     {"bbox": (18.90, 50.18, 19.15, 50.32), "teryt": "2469"},
    "Gdynia":       {"bbox": (18.42, 54.42, 18.62, 54.58), "teryt": "2262"},
    "Częstochowa":  {"bbox": (18.98, 50.75, 19.20, 50.87), "teryt": "2464"},
    "Radom":        {"bbox": (21.05, 51.36, 21.25, 51.48), "teryt": "1463"},
    "Toruń":        {"bbox": (18.50, 52.94, 18.72, 53.06), "teryt": "0463"},
    "Rzeszów":      {"bbox": (21.90, 50.00, 22.10, 50.10), "teryt": "1863"},
    "Kielce":       {"bbox": (20.55, 50.80, 20.75, 50.95), "teryt": "2661"},
    "Olsztyn":      {"bbox": (20.35, 53.72, 20.55, 53.85), "teryt": "2862"},
    "Zabrze":       {"bbox": (18.70, 50.25, 18.90, 50.38), "teryt": "2478"},
    "Sosnowiec":    {"bbox": (18.98, 50.20, 19.20, 50.32), "teryt": "2477"},
}

# WFS TYPENAMES — RCN typically has separate layers for lokale/budynki/dzialki.
# Names are guessed based on public WFS conventions; the script probes both plural
# and singular forms and adapts to whatever the endpoint returns.
LAYER_CANDIDATES: List[Tuple[str, str]] = [
    ("lokal",   "ms:rcn_lokale"),
    ("lokal",   "rcn:lokale"),
    ("lokal",   "lokale"),
    ("budynek", "ms:rcn_budynki"),
    ("budynek", "rcn:budynki"),
    ("budynek", "budynki"),
    ("dzialka", "ms:rcn_dzialki"),
    ("dzialka", "rcn:dzialki"),
    ("dzialka", "dzialki"),
]


# ------------------------------------------------------------------ mongo
def get_mongo():
    # Accept either MONGO_URL (our convention) or MONGODB_URI (Vercel/Atlas standard).
    url = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI")
    if not url:
        # Attempt to read from backend/.env for convenience.
        env_path = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
        if os.path.isfile(env_path):
            with open(env_path) as fh:
                for line in fh:
                    for key in ("MONGO_URL=", "MONGODB_URI="):
                        if line.startswith(key):
                            url = line.strip().split("=", 1)[1].strip('"').strip("'")
                            break
                    if url:
                        break
    if not url:
        log.error("MONGO_URL / MONGODB_URI is not set. Export it or put it in backend/.env")
        sys.exit(1)
    client = MongoClient(url, serverSelectionTimeoutMS=8000)
    # verify connection
    try:
        client.admin.command("ping")
    except Exception as e:
        log.error("MongoDB connection failed: %s", e)
        sys.exit(1)
    db_name = os.environ.get("MONGO_DB", "finderdom")
    return client[db_name]


def ensure_indexes(coll):
    coll.create_index(
        [("teryt", ASCENDING), ("data_zawarcia", ASCENDING),
         ("cena_brutto", ASCENDING), ("powierzchnia", ASCENDING)],
        name="rcn_dedup",
        unique=True,
    )
    coll.create_index([("city", ASCENDING), ("rodzaj_nieruchomosci", ASCENDING)])
    coll.create_index([("data_zawarcia", ASCENDING)])
    coll.create_index([("city", ASCENDING), ("rodzaj_nieruchomosci", ASCENDING),
                       ("data_zawarcia", ASCENDING)])


# ------------------------------------------------------------------ wfs client
def wfs_get_capabilities() -> Optional[str]:
    """Fetch WFS GetCapabilities to discover feature type names."""
    params = {"SERVICE": "WFS", "REQUEST": "GetCapabilities", "VERSION": "2.0.0"}
    try:
        r = requests.get(WFS_URL, params=params, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("GetCapabilities failed: %s", e)
        return None


def discover_typenames(capabilities: str) -> List[str]:
    """Extract feature type names from GetCapabilities XML."""
    names = re.findall(r"<Name>([^<]+)</Name>", capabilities)
    # WFS list looks like ['ms:rcn_lokale', 'ms:rcn_budynki', ...]
    return [n for n in names if "rcn" in n.lower() or n.lower() in ("lokale", "budynki", "dzialki")]


def wfs_fetch(typename: str, bbox: Tuple[float, float, float, float],
              limit: int = 5000, start: int = 0) -> Optional[dict]:
    """Fetch a batch of features from WFS as GeoJSON."""
    minx, miny, maxx, maxy = bbox
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "COUNT": limit,
        "STARTINDEX": start,
        "SRSNAME": "EPSG:4326",
        "BBOX": f"{miny},{minx},{maxy},{maxx},EPSG:4326",  # WFS 2.0 order = lat,lon
        "OUTPUTFORMAT": "application/json",
    }
    try:
        r = requests.get(WFS_URL, params=params, timeout=WFS_TIMEOUT)
        if r.status_code >= 400:
            log.debug("WFS %s -> HTTP %s (%s)", typename, r.status_code, r.text[:200])
            return None
        # Some servers return application/gml if json unsupported
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype and not r.text.lstrip().startswith("{"):
            log.debug("WFS %s -> non-JSON response (%s)", typename, ctype)
            return None
        return r.json()
    except Exception as e:
        log.debug("WFS fetch failed (%s): %s", typename, e)
        return None


# ------------------------------------------------------------------ parse
def _num(v: Any) -> Optional[float]:
    """Convert value to float, ignoring garbage."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _parse_props(props: Dict[str, Any], kind: str, city: str) -> Optional[Dict[str, Any]]:
    """
    Normalize a single RCN feature's properties to our schema.
    RCN attribute names differ per powiat schema — we probe multiple variants.
    """
    # Cena brutto — variants: cena_brutto, cena, cena_pln, cenaBrutto, cena_transakcji
    cena_keys = ("cena_brutto", "cenaBrutto", "cena_transakcji", "cena", "cena_pln",
                 "cena_calkowita", "brutto")
    cena = None
    for k in cena_keys:
        if k in props and props[k]:
            cena = _num(props[k])
            if cena and cena > 100:
                break
    if not cena or cena < 100:
        return None  # skip garbage entries

    # Powierzchnia — for lokale/budynki
    pow_keys = ("powierzchnia", "pow_lokalu", "pow_uzytkowa", "powierzchnia_lokalu",
                "powLokalu", "pow_calkowita", "pow_dzialki", "powierzchnia_dzialki")
    powierzchnia = None
    for k in pow_keys:
        if k in props and props[k]:
            powierzchnia = _num(props[k])
            if powierzchnia and powierzchnia > 0:
                break

    # Data zawarcia
    data = None
    for k in ("data_zawarcia", "dataZawarcia", "data_transakcji", "data", "dataAktu"):
        if k in props and props[k]:
            v = str(props[k])[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                data = v
                break
    if not data:
        return None

    # Rodzaj rynku
    rynek = None
    for k in ("rodzaj_rynku", "rynek", "typ_rynku"):
        if k in props and props[k]:
            v = str(props[k]).lower()
            if "pierwot" in v:
                rynek = "pierwotny"
            elif "wtórn" in v or "wtorn" in v:
                rynek = "wtórny"
            else:
                rynek = v[:20]
            break

    # TERYT
    teryt = None
    for k in ("teryt", "TERYT", "kod_teryt", "id_powiatu"):
        if k in props and props[k]:
            teryt = str(props[k])[:10]
            break

    doc = {
        "city": city,
        "rodzaj_nieruchomosci": kind,      # lokal | budynek | dzialka
        "cena_brutto": round(cena, 2),
        "powierzchnia": round(powierzchnia, 2) if powierzchnia else None,
        "cena_m2": round(cena / powierzchnia, 2) if powierzchnia and powierzchnia > 0 else None,
        "data_zawarcia": data,
        "rodzaj_rynku": rynek,
        "teryt": teryt or "",
        "raw_keys": list(props.keys())[:8],  # debug: what fields we saw
        "ingested_at": datetime.now(timezone.utc),
    }
    return doc


# ------------------------------------------------------------------ main
def ingest_city(coll, city: str, cfg: Dict[str, Any], typenames_by_kind: Dict[str, str],
                since: Optional[str]) -> Dict[str, int]:
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "errors": 0}
    for kind, typename in typenames_by_kind.items():
        log.info("  [%s] %s -> %s", city, kind, typename)
        start = 0
        batch_size = 2000
        while True:
            data = wfs_fetch(typename, cfg["bbox"], limit=batch_size, start=start)
            if not data or "features" not in data:
                break
            features = data["features"]
            if not features:
                break
            stats["fetched"] += len(features)
            docs = []
            for f in features:
                doc = _parse_props(f.get("properties") or {}, kind, city)
                if not doc:
                    stats["skipped"] += 1
                    continue
                if since and doc["data_zawarcia"] < since:
                    stats["skipped"] += 1
                    continue
                docs.append(doc)
            if docs:
                try:
                    result = coll.insert_many(docs, ordered=False)
                    stats["inserted"] += len(result.inserted_ids)
                except BulkWriteError as bwe:
                    # duplicates are fine — count them as skipped
                    inserted = bwe.details.get("nInserted", 0)
                    stats["inserted"] += inserted
                    stats["skipped"] += len(docs) - inserted
                except Exception as e:
                    log.warning("  insert error: %s", e)
                    stats["errors"] += 1
            log.info("    batch [%d..%d]: parsed=%d, inserted=%d",
                     start, start + len(features), len(docs), stats["inserted"])
            if len(features) < batch_size:
                break
            start += batch_size
            time.sleep(0.4)   # be polite
    return stats


def resolve_typenames(city_bbox: Tuple[float, float, float, float]) -> Dict[str, str]:
    """
    Probe candidate typenames against real WFS to find which ones actually work.
    Uses a small BBOX (Warszawa) to test with COUNT=1.
    """
    found: Dict[str, str] = {}
    for kind, tn in LAYER_CANDIDATES:
        if kind in found:
            continue
        # try count=1
        params = {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": tn, "COUNT": 1, "SRSNAME": "EPSG:4326",
            "BBOX": f"{city_bbox[1]},{city_bbox[0]},{city_bbox[3]},{city_bbox[2]},EPSG:4326",
            "OUTPUTFORMAT": "application/json",
        }
        try:
            r = requests.get(WFS_URL, params=params, timeout=25)
            if r.status_code == 200 and (r.text.lstrip().startswith("{") or "features" in r.text[:200]):
                found[kind] = tn
                log.info("Layer detected: %s -> %s", kind, tn)
        except Exception:
            continue
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", help="Only ingest this city (e.g. Warszawa)")
    ap.add_argument("--since", help="Only import transactions since YYYY-MM-DD (default: no filter)")
    ap.add_argument("--probe", action="store_true", help="Only probe WFS layers and exit")
    args = ap.parse_args()

    log.info("=== RCN ingest ===")
    log.info("WFS: %s", WFS_URL)

    # Try to detect available layers using a probe city
    probe_bbox = CITIES["Warszawa"]["bbox"]
    log.info("Probing WFS layers…")
    typenames = resolve_typenames(probe_bbox)
    if not typenames:
        log.error("Could not detect any RCN feature type via WFS.")
        log.error("The RCN WFS endpoint may currently be down or require captcha.")
        log.error("Try again in a few minutes, or use the QGIS plugin from https://plugins.qgis.org/plugins/pobierz_dane_GUGiK.")
        sys.exit(2)
    if args.probe:
        log.info("Detected: %s", typenames)
        sys.exit(0)

    db = get_mongo()
    coll = db["rcn_transactions"]
    ensure_indexes(coll)
    log.info("MongoDB collection: %s.rcn_transactions", db.name)

    cities = {args.city: CITIES[args.city]} if args.city and args.city in CITIES else CITIES
    if args.city and args.city not in CITIES:
        log.error("Unknown city '%s'. Known: %s", args.city, ", ".join(CITIES))
        sys.exit(1)

    grand_total = {"fetched": 0, "inserted": 0, "skipped": 0, "errors": 0}
    for city, cfg in cities.items():
        log.info("── %s ──", city)
        s = ingest_city(coll, city, cfg, typenames, args.since)
        for k in grand_total:
            grand_total[k] += s[k]
        log.info("  DONE %s: fetched=%d inserted=%d skipped=%d",
                 city, s["fetched"], s["inserted"], s["skipped"])
    log.info("=== TOTAL: fetched=%d inserted=%d skipped=%d errors=%d ===",
             grand_total["fetched"], grand_total["inserted"],
             grand_total["skipped"], grand_total["errors"])


if __name__ == "__main__":
    main()
