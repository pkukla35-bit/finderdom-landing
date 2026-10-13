#!/usr/bin/env python3
"""
RCN (Rejestr Cen Nieruchomości, GUGiK) → MongoDB `rcn_transactions`.

Źródło: publiczny WFS 2.0  https://mapy.geoportal.gov.pl/wss/service/rcn  (bezpłatny od 13.02.2026)
Warstwy:  ms:lokale  (mieszkania — pełne dane: cena, m², izby, kondygnacja, adres)
          ms:dzialki (działki niezabudowane — cena nieruchomości + pow. ewidencyjna)
Uwaga:    ms:budynki prawie nie ma BUD_POW_UZYT (pow. użytkowa domu), więc dla domów nie da się
          policzyć zł/m² — domy pomijamy (wycena domów zostaje na danych ofertowych).

Sprawdzone empirycznie (10.2026):
  • BBOX w FES musi być w EPSG:2180 w kolejności (northing, easting); w 4326 zwraca 0 wyników,
  • filtr DOK_DATA > 'YYYY-MM-DD' działa, paging COUNT/STARTINDEX działa,
  • 2000 rekordów ≈ 2 s.

Użycie:
  MONGODB_URI=... python scripts/rcn_sync.py                 # wszystkie miasta z scripts/city-coords.js
  MONGODB_URI=... python scripts/rcn_sync.py --city Kraków   # jedno miasto
  MONGODB_URI=... python scripts/rcn_sync.py --since 2025-01-01 --limit-cities 30
"""
from __future__ import annotations

import argparse, logging, math, os, re, sys, time
from datetime import datetime, timezone

import requests
from pymongo import MongoClient, UpdateOne, ASCENDING, DESCENDING
from pyproj import Transformer

log = logging.getLogger("rcn")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

WFS = "https://mapy.geoportal.gov.pl/wss/service/rcn"
PAGE = 2000
TIMEOUT = 180
TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
TO_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)

# Promień wokół centrum miasta (km). Duże miasta szerzej (aglomeracja), reszta 7 km.
BIG = {"warszawa": 18, "krakow": 14, "wroclaw": 13, "lodz": 13, "poznan": 13, "gdansk": 14, "szczecin": 12,
       "bydgoszcz": 10, "lublin": 10, "bialystok": 10, "katowice": 12, "gdynia": 10}
DEFAULT_RADIUS_KM = 7


def load_cities(path: str) -> dict:
    """city-coords.js → {"warszawa": (lat, lon), ...}"""
    txt = open(path, encoding="utf-8").read()
    return {k: (float(a), float(b)) for k, a, b in re.findall(r'"([^"]+)":\[([-\d.]+),([-\d.]+)\]', txt)}


def bbox_2180(lat: float, lon: float, km: float):
    """Zwraca (N1, E1, N2, E2) — EPSG:2180 w GML/WFS ma kolejność osi northing, easting
    (gml:pos = "485174 637907" dla Warszawy: N≈485k, E≈638k)."""
    dlat = km / 111.0
    dlon = km / (111.0 * math.cos(math.radians(lat)))
    e1, n1 = TO_2180.transform(lon - dlon, lat - dlat)
    e2, n2 = TO_2180.transform(lon + dlon, lat + dlat)
    return min(n1, n2), min(e1, e2), max(n1, n2), max(e1, e2)


def fes_filter(bbox, since: str, extra: str = "") -> str:
    n1, e1, n2, e2 = bbox
    return (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:gml="http://www.opengis.net/gml/3.2"><fes:And>'
        f'<fes:PropertyIsGreaterThan><fes:ValueReference>DOK_DATA</fes:ValueReference><fes:Literal>{since}</fes:Literal></fes:PropertyIsGreaterThan>'
        f'{extra}'
        '<fes:BBOX><fes:ValueReference>GEOMETRIA</fes:ValueReference>'
        '<gml:Envelope srsName="urn:ogc:def:crs:EPSG::2180">'
        f'<gml:lowerCorner>{n1:.0f} {e1:.0f}</gml:lowerCorner><gml:upperCorner>{n2:.0f} {e2:.0f}</gml:upperCorner>'
        '</gml:Envelope></fes:BBOX></fes:And></fes:Filter>'
    )


def fetch_layer(layer: str, flt: str):
    """Generator po surowych fragmentach XML feature'ów (paging)."""
    start = 0
    while True:
        params = {"SERVICE": "WFS", "REQUEST": "GetFeature", "VERSION": "2.0.0", "TYPENAMES": f"ms:{layer}",
                  "COUNT": PAGE, "STARTINDEX": start, "FILTER": flt}
        for attempt in range(3):
            try:
                r = requests.get(WFS, params=params, timeout=TIMEOUT)
                r.raise_for_status()
                break
            except Exception as e:
                log.warning("WFS %s start=%d attempt %d: %s", layer, start, attempt + 1, e)
                time.sleep(5 * (attempt + 1))
        else:
            return
        txt = r.text
        if "ExceptionReport" in txt[:500]:
            log.error("WFS exception: %s", txt[:300])
            return
        feats = re.findall(rf"<ms:{layer} gml:id[^>]*>(.*?)</ms:{layer}>", txt, re.S)
        for f in feats:
            yield f
        if len(feats) < PAGE:
            return
        start += PAGE


def val(f: str, key: str):
    m = re.search(rf"<ms:{key}>([^<]*)</ms:{key}>", f)
    return (m.group(1).strip() if m else "") or None


def num(f: str, key: str):
    v = val(f, key)
    try:
        return float(v.replace(",", ".")) if v else None
    except ValueError:
        return None


def date_of(f: str):
    m = re.search(r"<ms:DOK_DATA[^>]*><gml:timePosition>([^<]*)</gml:timePosition>", f)
    return m.group(1)[:10] if m else None


def pos_of(f: str):
    m = re.search(r"<gml:pos>([-\d.]+) ([-\d.]+)</gml:pos>", f)
    if not m:
        return None, None
    northing, easting = float(m.group(1)), float(m.group(2))
    lon, lat = TO_4326.transform(easting, northing)
    return round(lat, 6), round(lon, 6)


def addr_parts(s: str | None):
    """'MSC:Warszawa;UL:ulica Polna;NR_PORZ:10/14' → (miasto, ulica)"""
    if not s:
        return None, None
    d = dict(p.split(":", 1) for p in s.split(";") if ":" in p)
    ul = (d.get("UL") or "").replace("ulica ", "").replace("aleja ", "al. ").strip() or None
    return (d.get("MSC") or "").strip() or None, ul


def market_of(f: str):
    v = val(f, "TRAN_RODZAJ_RYNKU") or ""
    return "pierwotny" if "Pierwotny" in v else ("wtórny" if "Wtorny" in v else None)


def parse_lokal(f: str, fallback_city: str):
    if val(f, "LOK_FUNKCJA") != "mieszkalna":
        return None
    udz = val(f, "NIER_UDZIAL")
    if udz and udz != "1/1":
        return None  # sprzedaż udziału — cena nie odpowiada całemu lokalowi
    area = num(f, "LOK_POW_UZYT")
    price = num(f, "LOK_CENA_BRUTTO") or num(f, "NIER_CENA_BRUTTO") or num(f, "TRAN_CENA_BRUTTO")
    if not area or not price or area < 12 or area > 400:
        return None
    ppm = price / area
    if ppm < 1500 or ppm > 70000:
        return None
    gid = val(f, "gid")
    lat, lng = pos_of(f)
    city, street = addr_parts(val(f, "LOK_ADRES"))
    return {
        "gid": f"lokal-{gid}", "rodzaj_nieruchomosci": "lokal", "city": city or fallback_city,
        "ulica": street, "cena_brutto": round(price), "powierzchnia": round(area, 2), "cena_m2": round(ppm, 2),
        "data_zawarcia": date_of(f), "rodzaj_rynku": market_of(f), "izby": int(num(f, "LOK_LICZBA_IZB") or 0) or None,
        "kondygnacja": int(num(f, "LOK_NR_KOND") or 0) if num(f, "LOK_NR_KOND") is not None else None,
        "teryt": val(f, "TERYT"), "lat": lat, "lng": lng, "rodzaj_trans": val(f, "TRAN_RODZAJ_TRANS"),
    }


def parse_dzialka(f: str, fallback_city: str):
    if val(f, "NIER_RODZAJ") != "nieruchomoscGruntowaNiezabudowana":
        return None
    if (val(f, "DZI_SPOSOB_UZYT") or "") == "terenyKomunikacyjne":
        return None
    udz = val(f, "NIER_UDZIAL")
    if udz and udz != "1/1":
        return None
    area = num(f, "DZI_POW_EWID")
    price = num(f, "DZI_CENA_BRUTTO") or num(f, "NIER_CENA_BRUTTO") or num(f, "TRAN_CENA_BRUTTO")
    if not area or not price or area < 100 or area > 50000:
        return None
    ppm = price / area
    if ppm < 2 or ppm > 6000:
        return None
    gid = val(f, "gid")
    lat, lng = pos_of(f)
    city, street = addr_parts(val(f, "DZI_ADRES"))
    return {
        "gid": f"dzialka-{gid}", "rodzaj_nieruchomosci": "dzialka", "city": city or fallback_city,
        "ulica": street, "cena_brutto": round(price), "powierzchnia": round(area, 1), "cena_m2": round(ppm, 2),
        "data_zawarcia": date_of(f), "rodzaj_rynku": market_of(f),
        "przeznaczenie": val(f, "DZI_PRZEZN_WMPZP"), "sposob_uzyt": val(f, "DZI_SPOSOB_UZYT"),
        "teryt": val(f, "TERYT"), "lat": lat, "lng": lng, "rodzaj_trans": val(f, "TRAN_RODZAJ_TRANS"),
    }


def pretty_city(key: str) -> str:
    return key.replace("-", " ").title()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--city", default="", help="klucz z city-coords.js, np. krakow (lub nazwa, np. Kraków)")
    ap.add_argument("--limit-cities", type=int, default=0)
    ap.add_argument("--layers", default="lokale,dzialki")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    cities = load_cities(os.path.join(here, "city-coords.js"))
    if args.city:
        key = args.city.lower()
        key = key.translate(str.maketrans("ąćęłńóśźż", "acelnoszz")).replace(" ", "-")
        cities = {k: v for k, v in cities.items() if k == key}
        if not cities:
            sys.exit(f"Nie znam miasta {args.city}")
    items = list(cities.items())
    if args.limit_cities:
        items = items[: args.limit_cities]

    coll = None
    if not args.dry_run:
        db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
        coll = db["rcn_transactions"]
        coll.create_index([("gid", ASCENDING)], unique=True)
        coll.create_index([("rodzaj_nieruchomosci", ASCENDING), ("lat", ASCENDING), ("lng", ASCENDING)])
        coll.create_index([("rodzaj_nieruchomosci", ASCENDING), ("city", ASCENDING), ("data_zawarcia", DESCENDING)])

    t0 = time.time()
    total_up = 0
    parsers = {"lokale": parse_lokal, "dzialki": parse_dzialka}
    for i, (key, (lat, lon)) in enumerate(items, 1):
        km = BIG.get(key, DEFAULT_RADIUS_KM)
        bbox = bbox_2180(lat, lon, km)
        city_name = pretty_city(key)
        for layer in args.layers.split(","):
            layer = layer.strip()
            if layer not in parsers:
                continue
            extra = ('<fes:PropertyIsEqualTo><fes:ValueReference>LOK_FUNKCJA</fes:ValueReference>'
                     '<fes:Literal>mieszkalna</fes:Literal></fes:PropertyIsEqualTo>') if layer == "lokale" else ""
            docs, raw_n = {}, 0
            for f in fetch_layer(layer, fes_filter(bbox, args.since, extra)):
                raw_n += 1
                d = parsers[layer](f, city_name)
                if d:
                    docs[d["gid"]] = d
            log.info("[%d/%d] %-18s %-8s r=%2dkm  raw=%6d  ok=%6d", i, len(items), city_name, layer, km, raw_n, len(docs))
            if coll is not None and docs:
                now = datetime.now(timezone.utc)
                ops = [UpdateOne({"gid": g}, {"$set": {**d, "updated_at": now}, "$setOnInsert": {"ingested_at": now}}, upsert=True)
                       for g, d in docs.items()]
                for j in range(0, len(ops), 1000):
                    res = coll.bulk_write(ops[j:j + 1000], ordered=False)
                    total_up += res.upserted_count
    log.info("DONE in %.0fs — nowych rekordów: %d", time.time() - t0, total_up)
    if coll is not None:
        log.info("Łącznie w rcn_transactions: %d", coll.estimated_document_count())
        st = coll.database.command("collstats", "rcn_transactions")
        dbst = coll.database.command("dbstats")
        log.info("Rozmiar kolekcji: %.1f MB (+ indeksy %.1f MB); cała baza: %.1f MB",
                 st.get("size", 0) / 1e6, st.get("totalIndexSize", 0) / 1e6, dbst.get("dataSize", 0) / 1e6)


if __name__ == "__main__":
    main()
