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


def fetch_og_image(url: str, timeout: int = 10) -> tuple[str | None, str]:
    """Pobierz og:image z strony Otodom. Zwraca (image_url, error_or_ok)."""
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
            return None, f"HTTP {r.status_code}"
        html = r.text
        m = OG_IMAGE_RE.search(html) or OG_IMAGE_REV_RE.search(html)
        if not m:
            return None, "no og:image"
        img = m.group(1).strip()
        # Filtruj placeholdery Otodom
        if "no-thumbnail" in img.lower() or "placeholder" in img.lower():
            return None, "placeholder"
        if not img.startswith("http"):
            return None, "invalid url"
        return img, "ok"
    except requests.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, f"err: {type(e).__name__}"


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

    # Znajdz oferty z brakujacym obrazkiem
    filt = {
        "scraped_via": "scrapingbee",
        "$or": [{"image": None}, {"image": ""}, {"image": {"$exists": False}}],
    }
    if args.property:
        # np. "dzialka" → dokumenty gdzie type zaczyna sie od "dzia" lub "dom" lub "mieszk"
        prefix = args.property[:4]
        filt["type"] = {"$regex": f"^{prefix}", "$options": "i"}

    total = coll.count_documents(filt)
    log.info(f"Znaleziono {total} ofert bez obrazka (limit: {args.limit})")

    docs = list(coll.find(filt, {"_id": 1, "url": 1, "external_id": 1}).limit(args.limit))
    if not docs:
        log.info("Nic do zrobienia."); return

    log.info(f"Przetwarzam {len(docs)} ofert w {args.workers} watkach…")
    ops = []
    stats = {"ok": 0, "fail": 0}
    start = time.time()

    def process(doc):
        url = doc.get("url")
        if not url: return doc, None, "no url"
        img, status = fetch_og_image(url)
        return doc, img, status

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, d): d for d in docs}
        for i, fut in enumerate(as_completed(futures), 1):
            doc, img, status = fut.result()
            if img:
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"image": img, "image_backfilled_at": datetime.now(timezone.utc).isoformat()}}))
                stats["ok"] += 1
            else:
                stats["fail"] += 1
            if i % 100 == 0:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                log.info(f"  progress {i}/{len(docs)} | ok={stats['ok']} fail={stats['fail']} | {rate:.1f}/s")

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
    log.info(f"KONIEC: ok={stats['ok']} fail={stats['fail']} time={elapsed:.1f}s")


if __name__ == "__main__":
    main()
