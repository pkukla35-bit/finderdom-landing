#!/usr/bin/env python3
"""
🌍 Oferty zagraniczne — realting.com (Hiszpania, Włochy, USA, Portugalia na start). 28.11.2026

Osobna kolekcja `listings_foreign` (nie miesza się z polską wyszukiwarką, raportami, duplikatami, alertami).
Strona /zagranica (kraje → regiony/miasta → oferty), karta oferty przez /oferta?id=rt-<id> (fallback w /api/listing/{id}).

Źródło: listy realting.com/{kraj}/{kategoria}?sort=-created_at&page=N — 30 kart/str., server-side HTML,
cena od razu w PLN (data-price-PLN) + EUR/USD. Kategorie: apartments, houses, lands, commercial, property-to-rent.

Uruchomienie: samodzielnie (`python scripts/scrape_zagranica.py --pages 2`) albo z scrape_portals.py
(po zadaniu adresowo/mieszkanie/sprzedaz — co turę, 2 strony = najnowsze; pierwszy import: --pages 15).
Env: MONGODB_URI (lub MONGO_URL), MONGODB_DB
"""
import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_portals import _money, _txt  # noqa: E402

log = logging.getLogger("zagranica")


def fetch(url: str) -> Optional[str]:
    """realting.com odrzuca zwykłe `requests` (403 — fingerprint TLS); curl_cffi z impersonate=chrome przechodzi."""
    from curl_cffi import requests as cr
    for attempt in range(3):
        try:
            r = cr.get(url, impersonate="chrome", timeout=40, headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
            if r.status_code == 200 and len(r.text) > 3000: return r.text
            if r.status_code in (404, 410): return None
            log.warning("  HTTP %s %s — pauza", r.status_code, url); time.sleep(15 * (attempt + 1))
        except Exception as e:
            log.warning("  fetch %s: %s", url, str(e)[:120]); time.sleep(5)
    return None

COUNTRIES = {
    # slug realting → (ISO, nazwa PL, flaga)
    "spain": ("ES", "Hiszpania", "🇪🇸"),
    "italy": ("IT", "Włochy", "🇮🇹"),
    "united-states": ("US", "USA", "🇺🇸"),
    "portugal": ("PT", "Portugalia", "🇵🇹"),
}
CATEGORIES = {
    # segment → (typ, transakcja); typ None = z tytułu
    "apartments": ("mieszkanie", "sprzedaz"),
    "houses": ("dom", "sprzedaz"),
    "lands": ("dzialka", "sprzedaz"),
    "commercial": ("lokal", "sprzedaz"),
    "property-to-rent": (None, "wynajem"),
}
# realting z Accept-Language: pl serwuje polskie tytuły („Mieszkanie 2 pokoi”, „Willa 4 pokoi”, „Działka”, „Lokal komercyjny”)
TYPE_WORDS = [
    ("dzialka", r"działk|grunt|parcel|teren"),
    ("lokal", r"lokal|biur|sklep|hotel|magazyn|restaurac|komercyj|inwestycyj|obiekt|budynek"),
    ("dom", r"willa|dom|szereg|bungalow|chalet|domek|finca|rezydenc|zamek|gospodarstw|trullo|masseria|ranczo|kamienic"),
    ("mieszkanie", r"mieszkan|kawalerk|penthouse|apartament|studio|loft|dwupoziom|condo"),
]


def _price(s: Optional[str]) -> Optional[float]:
    """„759 766 zł” → 759766; „1,37M zł” → 1 370 000; „2,24B” → 2 240 000 000 (realting skraca duże kwoty)."""
    if not s: return None
    m = re.search(r"([\d\s\xa0.,]+)\s*([MBK])?", s.replace("\u00a0", " "))
    if not m: return None
    num = m.group(1).strip().replace(" ", "")
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2) or "", 1)
    if mult > 1: num = num.replace(",", ".")
    else: num = num.replace(",", ".") if re.search(r",\d{1,2}$", num) else num.replace(",", "").replace(".", "")
    try: return round(float(num) * mult)
    except ValueError: return None


def _type_from_title(t: str, default: str = "mieszkanie") -> str:
    tl = t.lower()
    for typ, rx in TYPE_WORDS:
        if re.search(rx, tl): return typ
    return default


def _title_pl(title: str, area: Optional[float], city: str, cname: str) -> str:
    parts = [re.sub(r"\s+", " ", title).strip()]
    if area: parts.append(f"{area:g} m²")
    return " · ".join(parts) + f" — {city}, {cname}"


def parse_cards(html: str, country_slug: str, cat: str) -> List[Dict[str, Any]]:
    iso, cname, flag = COUNTRIES[country_slug]
    typ_default, trans = CATEGORIES[cat]
    out = []
    for m in re.finditer(r'<div class="d-flex flex-column teaser-tile" data-id="(\d+)">(.*?)<div class="social-actions">', html, re.S):
        oid, c = m.group(1), m.group(2)
        url = re.search(r'href="(https://realting\.com/(?:pl/)?[a-z-]+/[a-z-]+/\d+)"', c)
        if not url: continue
        title = re.search(r'<div class="teaser-title[^"]*">([^<]*)</div>', c)
        route = re.search(r'<div class="route">([^<]*)</div>', c)
        img = re.search(r'<meta itemprop="contentUrl" content="([^"]+)"', c) or re.search(r'<img class="w-100p" src="([^"]+)"', c)
        desc = re.search(r'<div class="clamp-3">(.*?)</div>', c, re.S)
        pln = re.search(r'data-price-PLN="([^"]*)"', c); eur = re.search(r'data-price-EUR="([^"]*)"', c); usd = re.search(r'data-price-USD="([^"]*)"', c)
        area = re.search(r'title="(?:Powierzchnia|Area)">.*?<span>([\d\s.,]+)\s*m²', c, re.S)
        rooms = re.search(r'title="(?:Pokoje|Rooms)">.*?<span>(\d+)</span>', c, re.S)
        beds = re.search(r'title="(?:Sypialnie|Bedrooms count)">.*?<span>(\d+)</span>', c, re.S)
        baths = re.search(r'title="(?:Liczba łazienek|Bathrooms count)">.*?<span>(\d+)</span>', c, re.S)
        floor = re.search(r'title="(?:Piętro|Floor)">.*?<span>([^<]+)</span>', c, re.S)
        t = _txt(title.group(1)) if title else ""
        rt = _txt(route.group(1)) if route else ""
        loc = [x.strip() for x in rt.split(",") if x.strip()]
        city = loc[0] if loc else cname
        region = loc[1] if len(loc) > 2 else None
        price_pln = _price(pln.group(1)) if pln else None
        area_v = _money(area.group(1)) if area else None
        typ = typ_default or _type_from_title(t)
        if trans == "sprzedaz" and typ_default is None: typ = _type_from_title(t)
        out.append({
            "source": "realting", "external_id": f"rt-{oid}", "url": url.group(1),
            "title": _title_pl(t, area_v, city, cname), "title_orig": t,
            "type": typ, "transaction_type": trans,
            "price": price_pln, "price_pln": price_pln,
            "price_eur": _price(eur.group(1)) if eur else None, "price_usd": _price(usd.group(1)) if usd else None,
            "currency_orig": "USD" if iso == "US" else "EUR",
            "price_pm2": round(price_pln / area_v) if price_pln and area_v and area_v > 5 else None,
            "area_m2": area_v, "rooms": int(rooms.group(1)) if rooms else ((int(beds.group(1)) + 1) if beds else None), "bedrooms": int(beds.group(1)) if beds else None,
            "bathrooms": int(baths.group(1)) if baths else None, "floor": _txt(floor.group(1)) if floor else None,
            "city": city, "region": region, "country": iso, "country_name": cname, "country_flag": flag, "location": rt,
            "image": img.group(1) if img else None, "images": [img.group(1)] if img else [],
            "description": _txt(desc.group(1)).replace("…", "").strip() if desc else "",
            "seller_type": "posrednik", "agency_name": None,
            "is_stale": False, "last_seen_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url: log.error("MONGODB_URI not set"); sys.exit(1)
    c = MongoClient(url, serverSelectionTimeoutMS=8000); c.admin.command("ping")
    return c[os.environ.get("MONGODB_DB") or "finderdom"]


def run(pages: int = 2, countries: Optional[List[str]] = None, cats: Optional[List[str]] = None, delay: float = 1.2, coll=None) -> Dict[str, int]:
    coll = coll if coll is not None else get_mongo()["listings_foreign"]
    try:
        coll.create_index([("source", 1), ("external_id", 1)], unique=True)
        coll.create_index([("country", 1), ("transaction_type", 1), ("type", 1), ("added_at", -1)])
        coll.create_index([("country", 1), ("city", 1)])
    except Exception as e:
        log.warning("index: %s", str(e)[:120])
    tot = {"new": 0, "updated": 0, "pages": 0}
    for cs in (countries or list(COUNTRIES)):
        for cat in (cats or list(CATEGORIES)):
            zero = 0
            for page in range(1, pages + 1):
                url = f"https://realting.com/{cs}/{cat}?sort=-created_at" + (f"&page={page}" if page > 1 else "")
                html = fetch(url)
                if not html: log.warning("  brak HTML %s", url); break
                items = parse_cards(html, cs, cat)
                tot["pages"] += 1
                if not items: break
                now = datetime.now(timezone.utc).isoformat()
                ops = [UpdateOne({"source": "realting", "external_id": d["external_id"]},
                                 {"$set": d, "$setOnInsert": {"added_at": now, "scraped_via": "zagranica"}}, upsert=True) for d in items]
                r = coll.bulk_write(ops, ordered=False)
                tot["new"] += r.upserted_count; tot["updated"] += r.modified_count
                log.info("  %s/%s p%d: %d kart → new=%d upd=%d", cs, cat, page, len(items), r.upserted_count, r.modified_count)
                zero = zero + 1 if r.upserted_count == 0 else 0
                if zero >= 2 and page >= 2: break
                time.sleep(delay)
    log.info("═══ DONE zagranica: new=%d updated=%d pages=%d ═══", tot["new"], tot["updated"], tot["pages"])
    return tot


def mark_stale(coll=None, days: int = 10) -> int:
    """Oferta niewidziana na listach „najnowsze” przez N dni nie znaczy zdjęta (listy to tylko czubek) — stale-check robimy
    przez stronę oferty: 404/410 lub brak ceny → is_stale. Limit 150/run, najstarsze last_checked."""
    coll = coll if coll is not None else get_mongo()["listings_foreign"]
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    n = 0
    for d in coll.find({"is_stale": {"$ne": True}, "last_seen_at": {"$lt": cutoff},
                        "$or": [{"stale_checked_at": {"$exists": False}}, {"stale_checked_at": {"$lt": cutoff}}]}, {"url": 1}).limit(150):
        html = fetch(d["url"]); time.sleep(0.8)
        now = datetime.now(timezone.utc).isoformat()
        gone = html is None or "data-price-PLN" not in html and "price-item" not in html
        coll.update_one({"_id": d["_id"]}, {"$set": ({"is_stale": True, "stale_since": now, "stale_reason": "gone"} if gone else {}) | {"stale_checked_at": now}})
        n += int(gone)
    log.info("zagranica stale-check: zdjęte=%d", n)
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--countries", default="")
    ap.add_argument("--cats", default="")
    ap.add_argument("--stale", action="store_true")
    a = ap.parse_args()
    run(a.pages, [x for x in a.countries.split(",") if x] or None, [x for x in a.cats.split(",") if x] or None)
    if a.stale: mark_stale()
