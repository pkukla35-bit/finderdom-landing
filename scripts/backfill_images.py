#!/usr/bin/env python3
"""
Backfill obrazków dla scraped ofert które mają image=null.

Iteruje po dokumentach w MongoDB z {scraped_via:'scrapingbee', image:null},
pobiera stronę Otodom, wyciąga og:image z meta tagów, aktualizuje dokument.

Uruchamiane po głównym scrape'ze - uzupełnia braki obrazków.
Nie używa ScrapingBee (Otodom nie blokuje pojedynczych GET z rotujących IP GitHub Actions).
"""
import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from pymongo import MongoClient, UpdateOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("backfill")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
OG_IMAGE_REV_RE = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)
OG_DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
OG_DESC_REV_RE = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', re.IGNORECASE)
NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]+?)</script>', re.IGNORECASE)

# Otodom zwraca w Next Data pole "typeLandName" po polsku lub "typeLand" po angielsku
NEXT_LAND_TYPE_RE = re.compile(r'"(?:typeLand|typeLandName|landType|purposeType|estateType|buildingType|dzialkaType)"\s*:\s*"([^"]+)"', re.IGNORECASE)
# Slowa w opisie/tytule sekcji "Rodzaj działki"
RODZAJ_DZIALKI_RE = re.compile(r'rodzaj[^<:]{0,20}?dzia[lł]ki?[^<]{0,10}?[:<][^<]{0,60}?(budowl|roln|rekreacyj|leśn|lesn|inwestycyj|komercyj|usług|uslug|siedlisk)', re.IGNORECASE)

DZIALKA_KEYWORDS = [
    ("budowlana",    [r"budowlan"]),
    ("rolna",        [r"\brolna\b", r"\brolne\b", r"\brolny\b", r"\borna\b", r"uprawn"]),
    ("rekreacyjna",  [r"rekreacyj", r"letnisk", r"weekend", r"\brod\b", r"ogrodow"]),
    ("lesna",        [r"lesn", r"leśn", r"\blasu\b", r"w lesie", r"zalesion"]),
    ("inwestycyjna", [r"inwestycyj", r"komercyj", r"usługow", r"uslugow", r"przemyslow", r"przemysłow"]),
]

# Mapowanie Otodom API typeLand → nasze
OTODOM_LAND_MAP = {
    "buildingland": "budowlana", "building_land": "budowlana", "budowlana": "budowlana",
    "agriculturalland": "rolna", "agricultural_land": "rolna", "rolna": "rolna",
    "recreationalland": "rekreacyjna", "recreational_land": "rekreacyjna", "rekreacyjna": "rekreacyjna",
    "forestland": "lesna", "forest_land": "lesna", "leśna": "lesna", "lesna": "lesna",
    "investmentland": "inwestycyjna", "commercialland": "inwestycyjna", "commercial_land": "inwestycyjna",
    "usługowa": "inwestycyjna", "uslugowa": "inwestycyjna", "inwestycyjna": "inwestycyjna",
    "otherland": None, "siedliskowa": None,
}

def detect_purpose(text: str) -> str | None:
    if not text: return None
    t = text.lower()
    for label, kws in DZIALKA_KEYWORDS:
        for kw in kws:
            if re.search(kw, t):
                return label
    return None


def fetch_page(url: str, timeout: int = 10) -> tuple[str | None, str | None, str | None, str]:
    """Pobierz stronę Otodom. Zwraca (og:image, og:description, land_purpose_from_json, status)."""
    try:
        import random
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "DNT": "1",
        }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None, None, None, f"HTTP {r.status_code}"
        html = r.text
        # Image
        m_img = OG_IMAGE_RE.search(html) or OG_IMAGE_REV_RE.search(html)
        img = m_img.group(1).strip() if m_img else None
        if img and ("no-thumbnail" in img.lower() or "placeholder" in img.lower() or not img.startswith("http")):
            img = None
        # Description
        m_desc = OG_DESC_RE.search(html) or OG_DESC_REV_RE.search(html)
        desc = m_desc.group(1).strip() if m_desc else None
        # Land purpose z __NEXT_DATA__ JSON (najbardziej wiarygodne)
        purpose_from_json = None
        m_next = NEXT_DATA_RE.search(html)
        if m_next:
            for m in NEXT_LAND_TYPE_RE.finditer(m_next.group(1)):
                v = m.group(1).lower().strip()
                mapped = OTODOM_LAND_MAP.get(v) or detect_purpose(v)
                if mapped:
                    purpose_from_json = mapped
                    break
        # Fallback: sekcja "Rodzaj działki" w HTML
        if not purpose_from_json:
            m_rodzaj = RODZAJ_DZIALKI_RE.search(html)
            if m_rodzaj:
                key = m_rodzaj.group(1).lower()
                purpose_from_json = detect_purpose(key)
        return img, desc, purpose_from_json, "ok"
    except requests.Timeout:
        return None, None, None, "timeout"
    except Exception as e:
        return None, None, None, f"err: {type(e).__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500, help="Max liczba ofert do przetworzenia")
    ap.add_argument("--workers", type=int, default=8, help="Liczba równoległych requestów")
    ap.add_argument("--property", type=str, default="", help="Filtruj po typie (dzialka/dom/mieszkanie)")
    args = ap.parse_args()

    mongo_uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not mongo_uri:
        log.error("Brak MONGODB_URI"); sys.exit(1)
    db_name = os.environ.get("MONGODB_DB", "finderdom")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[db_name]
    coll = db.listings

    # Znajdz oferty do przetworzenia:
    # - brak obrazka LUB (dzialka + brak dzialka_type)
    filt = {"scraped_via": "scrapingbee"}
    if args.property:
        prefix = args.property[:4]
        filt["type"] = {"$regex": f"^{prefix}", "$options": "i"}
    filt["$or"] = [
        {"image": None}, {"image": ""}, {"image": {"$exists": False}},
        # dzialki bez sklasyfikowanego przeznaczenia
        {"$and": [
            {"type": {"$regex": "^dzia", "$options": "i"}},
            {"$or": [
                {"dzialka_type": None},
                {"dzialka_type": ""},
                {"dzialka_type": {"$in": ["inne", "siedliskowa"]}},
                {"dzialka_type": {"$exists": False}},
            ]}
        ]}
    ]

    total = coll.count_documents(filt)
    log.info(f"Znaleziono {total} ofert do backfillu (limit: {args.limit})")

    docs = list(coll.find(filt, {"_id": 1, "url": 1, "external_id": 1, "type": 1, "image": 1, "dzialka_type": 1, "title": 1}).limit(args.limit))
    if not docs:
        log.info("Nic do zrobienia."); return

    log.info(f"Przetwarzam {len(docs)} ofert w {args.workers} watkach…")
    ops = []
    stats = {"img_ok": 0, "purpose_ok": 0, "fail": 0}
    start = time.time()

    def process(doc):
        url = doc.get("url")
        if not url: return doc, None, None, None, "no url"
        img, desc, purpose_json, status = fetch_page(url)
        return doc, img, desc, purpose_json, status

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, d): d for d in docs}
        for i, fut in enumerate(as_completed(futures), 1):
            doc, img, desc, purpose_json, status = fut.result()
            update = {}
            # Obrazek (tylko jeśli brakuje)
            if img and not doc.get("image"):
                update["image"] = img
                stats["img_ok"] += 1
            # Przeznaczenie dzialki
            is_dzialka = str(doc.get("type","")).lower().startswith("dzia")
            current_dt = doc.get("dzialka_type")
            needs_purpose = is_dzialka and (not current_dt or current_dt in ("inne", "siedliskowa"))
            if needs_purpose:
                # 1) __NEXT_DATA__ (najbardziej wiarygodne)
                purpose = purpose_json
                # 2) title + description keywords
                if not purpose:
                    combined = (doc.get("title") or "") + " " + (desc or "")
                    purpose = detect_purpose(combined)
                if purpose:
                    update["dzialka_type"] = purpose
                    stats["purpose_ok"] += 1
            if update:
                update["backfilled_at"] = datetime.now(timezone.utc).isoformat()
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": update}))
            else:
                stats["fail"] += 1
            if i % 100 == 0:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                log.info(f"  progress {i}/{len(docs)} | img={stats['img_ok']} purpose={stats['purpose_ok']} fail={stats['fail']} | {rate:.1f}/s")

    # Batch update MongoDB
    if ops:
        for i in range(0, len(ops), 200):
            batch = ops[i:i+200]
            try:
                r = coll.bulk_write(batch, ordered=False)
                log.info(f"  bulk_write batch: modified={r.modified_count}")
            except Exception as e:
                log.warning(f"  bulk_write error: {e}")

    elapsed = time.time() - start
    log.info(f"KONIEC: img_ok={stats['img_ok']} purpose_ok={stats['purpose_ok']} fail={stats['fail']} time={elapsed:.1f}s")


if __name__ == "__main__":
    main()
