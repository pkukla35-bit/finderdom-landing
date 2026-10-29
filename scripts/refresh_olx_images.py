#!/usr/bin/env python3
"""
Odświeżanie zdjęć OLX — GitHub Actions, 0 zł.

Problem: OLX co jakiś czas ZMIENIA adresy zdjęć aktywnych ogłoszeń (…/v1/files/<id>-PL/image). Stare linki
zwracają 404 z szarym logo OLX, choć ogłoszenie nadal istnieje. Zdjęcia zaimportowane raz (Apify / scraper)
po kilku tygodniach „znikają” z kart w wyszukiwarce.

Co robi:
  1. Usuwa duplikaty tej samej oferty OLX w bazie (stary rekord source=apify + nowy source=olx) — zostaje świeższy.
  2. Dla każdej aktywnej oferty OLX (najdawniej sprawdzane najpierw) robi HEAD na 1. zdjęcie:
       200 → OK, zapisuje olx_img_checked_at;
       404/inny → pobiera publiczne API OLX /api/v1/offers/<id>/:
           status active → podmienia image/images (+cena) na świeże;
           404 „Ad not found” / status != active → is_stale=True (stale_reason=olx_gone) — sygnałem jest samo
           ogłoszenie, NIE brak zdjęcia.
  3. 403/429 z OLX → pauza, po 3 z rzędu przerywa (reszta następnym razem).

Użycie: python scripts/refresh_olx_images.py [--limit 1500] [--delay 0.8] [--dry-run]
"""
import argparse, logging, os, sys, time
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_olx import get_mongo, _num  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("olx-img")

from curl_cffi import requests as creq  # type: ignore

S = creq.Session(impersonate="chrome")
HDR = {"Accept-Language": "pl-PL,pl;q=0.9", "Accept": "application/json,text/html,*/*;q=0.8"}


def head_ok(url: str) -> bool:
    try:
        r = S.head(url, timeout=20, allow_redirects=True)
        return r.status_code == 200 and "image" in (r.headers.get("content-type") or "")
    except Exception:
        return False


def fetch_ad(ext_id: str):
    """→ ('active', data) | ('gone', None) | ('blocked', None) | ('error', None)"""
    try:
        r = S.get(f"https://www.olx.pl/api/v1/offers/{ext_id}/", headers=HDR, timeout=30)
    except Exception as e:
        log.warning("  api error %s: %s", ext_id, str(e)[:80]); return "error", None
    if r.status_code == 404: return "gone", None
    if r.status_code in (403, 429): return "blocked", None
    if r.status_code != 200:
        log.warning("  api %s → HTTP %s", ext_id, r.status_code); return "error", None
    try:
        data = r.json().get("data") or {}
    except Exception:
        return "error", None
    if (data.get("status") or "active") != "active": return "gone", None
    return "active", data


def photos_of(data: dict):
    out = []
    for ph in data.get("photos") or []:
        link = ph.get("link") if isinstance(ph, dict) else ph
        if isinstance(link, str): out.append(link.replace("{width}x{height}", "1080x720"))
    return out


def price_of(data: dict):
    for p in data.get("params") or []:
        if p.get("key") == "price":
            v = p.get("value") or {}
            return _num(v.get("value")) if isinstance(v, dict) else _num(v)
    return None


def dedupe_apify(coll, dry: bool) -> int:
    """Ta sama oferta OLX 2× (source=apify + source=olx) → usuń rekord apify, zostaje świeższy z olx-direct."""
    pipeline = [
        {"$match": {"url": {"$regex": r"olx\.pl/"}, "source": {"$in": ["apify", "olx"]}}},
        {"$group": {"_id": "$external_id", "srcs": {"$addToSet": "$source"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}, "srcs": {"$all": ["apify", "olx"]}}},
    ]
    ids = [g["_id"] for g in coll.aggregate(pipeline, allowDiskUse=True)]
    if not ids: return 0
    log.info("Duplikaty OLX apify+olx-direct: %d", len(ids))
    if dry: return len(ids)
    deleted = 0
    for i in range(0, len(ids), 500):
        deleted += coll.delete_many({"source": "apify", "external_id": {"$in": ids[i:i + 500]}, "url": {"$regex": r"olx\.pl/"}}).deleted_count
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500); ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--recheck-days", type=int, default=5, help="nie sprawdzaj ponownie ofert sprawdzonych w ciągu N dni")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    coll = get_mongo()["listings"]

    removed = dedupe_apify(coll, a.dry_run)
    log.info("Usunięte duplikaty apify: %d", removed)

    since_dt = datetime.now(timezone.utc) - timedelta(days=a.recheck_days)
    since = since_dt.isoformat()
    # olx_img_checked_at bywa stringiem ISO (ten skrypt) lub BSON Date (starszy mechanizm) — porównujemy oba typy
    q = {"url": {"$regex": r"olx\.pl/"}, "is_stale": {"$ne": True},
         "$or": [{"olx_img_checked_at": {"$exists": False}}, {"olx_img_checked_at": {"$lt": since}},
                 {"olx_img_checked_at": {"$lt": since_dt}}]}
    cur = coll.find(q, {"external_id": 1, "image": 1, "images": 1, "source": 1, "olx_img_checked_at": 1}) \
              .sort("olx_img_checked_at", 1).limit(a.limit)
    docs = list(cur)
    log.info("Do sprawdzenia: %d ofert OLX (limit %d, pozostałe w bazie: %d)", len(docs), a.limit, coll.count_documents(q))
    st = {"ok": 0, "refreshed": 0, "gone": 0, "error": 0, "blocked": 0}
    blocked_streak = 0
    for i, d in enumerate(docs, 1):
        now = datetime.now(timezone.utc).isoformat()
        img = d.get("image") or ((d.get("images") or [None])[0])
        ext = str(d.get("external_id") or "")
        if img and head_ok(img):
            st["ok"] += 1
            if not a.dry_run: coll.update_one({"_id": d["_id"]}, {"$set": {"olx_img_checked_at": now}})
            continue
        if not ext.isdigit():
            st["error"] += 1; continue
        status, data = fetch_ad(ext)
        time.sleep(a.delay)
        if status == "blocked":
            blocked_streak += 1; st["blocked"] += 1
            log.warning("  OLX 403/429 (%d/3) — pauza 60s", blocked_streak); time.sleep(60)
            if blocked_streak >= 3: log.error("Blokada OLX — przerywam, reszta następnym razem"); break
            continue
        blocked_streak = 0
        if status == "active":
            photos = photos_of(data)
            upd = {"olx_img_checked_at": now, "last_seen_at": now}
            if photos: upd["image"], upd["images"] = photos[0], photos[:12]
            p = price_of(data)
            if p and p > 1000: upd["price"] = p
            st["refreshed"] += 1
            log.info("  %s odświeżone: %d zdjęć%s", ext, len(photos), "" if not p else f", cena {p:.0f}")
            if not a.dry_run:
                coll.update_one({"_id": d["_id"]}, {"$set": upd, "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}})
        elif status == "gone":
            st["gone"] += 1
            log.info("  %s nieaktywne na OLX → is_stale", ext)
            if not a.dry_run:
                coll.update_one({"_id": d["_id"]}, {"$set": {"is_stale": True, "stale_since": now, "stale_reason": "olx_gone", "olx_img_checked_at": now}})
        else:
            st["error"] += 1
        if i % 100 == 0: log.info("… %d/%d %s", i, len(docs), st)
    log.info("═══ DONE OLX zdjęcia: %s, usunięte duplikaty apify=%d%s ═══", st, removed, " (DRY RUN)" if a.dry_run else "")


if __name__ == "__main__":
    main()
