#!/usr/bin/env python3
"""
Scrape działki (plots) from Otodom via ScrapingBee → MongoDB.

- Uses Otodom's Next.js __NEXT_DATA__ JSON (no JS render needed → 1 credit/page)
- 20 top cities × ~15 pages ≈ 300 requests ≈ 300 credits
- Idempotent: dedupes by (source, external_id)

Usage:
    export SCRAPINGBEE_API_KEY=xxx
    export MONGODB_URI="mongodb+srv://..."
    python scripts/scrape_dzialki.py                 # all 20 cities
    python scripts/scrape_dzialki.py --city warszawa
    python scripts/scrape_dzialki.py --max-pages 3   # small test
"""
from __future__ import annotations
import argparse, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from pymongo import MongoClient, ASCENDING
from pymongo.errors import BulkWriteError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("dzialki")

SB_URL = "https://app.scrapingbee.com/api/v1/"

# (voivodeship-slug, city-slug) for Otodom URL:
# https://www.otodom.pl/pl/wyniki/sprzedaz/dzialka/{voi}/{city}/{city}?limit=72&page=N
CITIES: List[Dict[str, str]] = [
    {"name": "Warszawa",    "voi": "mazowieckie",       "slug": "warszawa/warszawa/warszawa"},
    {"name": "Kraków",      "voi": "malopolskie",       "slug": "krakow/krakow/krakow"},
    {"name": "Wrocław",     "voi": "dolnoslaskie",      "slug": "wroclaw/wroclaw/wroclaw"},
    {"name": "Łódź",        "voi": "lodzkie",           "slug": "lodz/lodz/lodz"},
    {"name": "Poznań",      "voi": "wielkopolskie",     "slug": "poznan/poznan/poznan"},
    {"name": "Gdańsk",      "voi": "pomorskie",         "slug": "gdansk/gdansk/gdansk"},
    {"name": "Szczecin",    "voi": "zachodniopomorskie","slug": "szczecin/szczecin/szczecin"},
    {"name": "Bydgoszcz",   "voi": "kujawsko--pomorskie","slug": "bydgoszcz/bydgoszcz/bydgoszcz"},
    {"name": "Lublin",      "voi": "lubelskie",         "slug": "lublin/lublin/lublin"},
    {"name": "Białystok",   "voi": "podlaskie",         "slug": "bialystok/bialystok/bialystok"},
    {"name": "Katowice",    "voi": "slaskie",           "slug": "katowice/katowice/katowice"},
    {"name": "Gdynia",      "voi": "pomorskie",         "slug": "gdynia/gdynia/gdynia"},
    {"name": "Częstochowa", "voi": "slaskie",           "slug": "czestochowa/czestochowa/czestochowa"},
    {"name": "Radom",       "voi": "mazowieckie",       "slug": "radom/radom/radom"},
    {"name": "Toruń",       "voi": "kujawsko--pomorskie","slug": "torun/torun/torun"},
    {"name": "Rzeszów",     "voi": "podkarpackie",      "slug": "rzeszow/rzeszow/rzeszow"},
    {"name": "Kielce",      "voi": "swietokrzyskie",    "slug": "kielce/kielce/kielce"},
    {"name": "Olsztyn",     "voi": "warminsko--mazurskie","slug": "olsztyn/olsztyn/olsztyn"},
    {"name": "Zabrze",      "voi": "slaskie",           "slug": "zabrze/zabrze/zabrze"},
    {"name": "Sosnowiec",   "voi": "slaskie",           "slug": "sosnowiec/sosnowiec/sosnowiec"},
]


def otodom_url(city: Dict[str, str], page: int) -> str:
    return f"https://www.otodom.pl/pl/wyniki/sprzedaz/dzialka/{city['voi']}/{city['slug']}?limit=72&page={page}"


def fetch_via_scrapingbee(url: str, api_key: str) -> Optional[str]:
    params = {
        "api_key": api_key,
        "url": url,
        "render_js": "false",     # SSR — 1 credit
        "premium_proxy": "false",
        "country_code": "pl",
    }
    try:
        r = requests.get(SB_URL, params=params, timeout=60)
        if r.status_code == 200:
            return r.text
        log.warning("SB HTTP %s for %s: %s", r.status_code, url, r.text[:200])
    except Exception as e:
        log.warning("SB fetch error: %s", e)
    return None


NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def parse_listings(html: str) -> List[Dict[str, Any]]:
    m = NEXT_DATA_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        log.debug("JSON parse fail: %s", e)
        return []
    # Path: props.pageProps.data.searchAds.items or similar
    def walk(node):
        if isinstance(node, dict):
            if "items" in node and isinstance(node["items"], list) and node["items"]:
                first = node["items"][0]
                if isinstance(first, dict) and ("id" in first or "slug" in first):
                    return node["items"]
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r:
                    return r
        return None
    items = walk(data) or []
    return [x for x in items if isinstance(x, dict)]


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except Exception:
        return None


def normalize(item: Dict[str, Any], city_name: str) -> Optional[Dict[str, Any]]:
    slug = item.get("slug") or item.get("url")
    ext_id = str(item.get("id") or item.get("advertId") or slug or "")
    if not ext_id or not slug:
        return None
    url = f"https://www.otodom.pl/pl/oferta/{slug}" if not slug.startswith("http") else slug

    # ── Detect subtype: budowlana / rolna / inne (as requested) ──
    title = item.get("title") or "Działka"
    text_blob = f"{title} {item.get('shortDescription', '')} {item.get('description', '')}".lower()
    ot_type = str(item.get("dzialkaType") or item.get("estateType") or "").lower()
    combined = f"{ot_type} {text_blob}"

    if "budowlan" in combined:
        subtype = "budowlana"
    elif "roln" in combined:
        subtype = "rolna"
    elif "rekreacyj" in combined:
        subtype = "rekreacyjna"
    elif "leśn" in combined or "lesn" in combined:
        subtype = "lesna"
    elif "usługow" in combined or "uslugow" in combined or "komercyj" in combined or "inwestycyj" in combined:
        subtype = "inwestycyjna"
    else:
        subtype = "inne"

    prop_type = f"działka {subtype}"

    # ── Real Otodom publication date (dateCreated / createdAtFirst / createdAt) ──
    date_raw = item.get("createdAtFirst") or item.get("dateCreated") or item.get("createdAt") or ""
    posted_iso = None
    if date_raw:
        s = str(date_raw).replace("T", " ").replace("Z", "")[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                posted_iso = dt.isoformat()
                break
            except (ValueError, TypeError):
                pass

    # price
    price = None
    total_price = item.get("totalPrice") or {}
    if isinstance(total_price, dict):
        price = _num(total_price.get("value"))
    if not price:
        price = _num(item.get("price"))

    # area
    area_m2 = _num(item.get("areaInSquareMeters")) or _num(item.get("area"))
    # location
    loc_data = item.get("location") or {}
    location = ""
    if isinstance(loc_data, dict):
        addr = loc_data.get("address") or {}
        if isinstance(addr, dict):
            city_part = (addr.get("city") or {}).get("name", "") if isinstance(addr.get("city"), dict) else ""
            district = (addr.get("district") or {}).get("name", "") if isinstance(addr.get("district"), dict) else ""
            location = ", ".join(x for x in [city_part, district] if x)
    if not location:
        location = city_name

    # coords
    coords = loc_data.get("coordinates") if isinstance(loc_data, dict) else None
    lat = _num((coords or {}).get("latitude"))
    lon = _num((coords or {}).get("longitude"))

    # image
    images = item.get("images") or []
    img_url = None
    if images and isinstance(images[0], dict):
        img_url = images[0].get("large") or images[0].get("medium") or images[0].get("small")

    # Seller type (pośrednik vs prywatna vs deweloper)
    seller_type = "posrednik"
    if item.get("isPrivateOwner"):
        seller_type = "prywatna"
    elif item.get("isDeveloperOwner"):
        seller_type = "deweloper"
    else:
        adv_type = str(item.get("extendedAdvertiserType") or item.get("advertiserType") or item.get("advertType") or "").upper()
        if adv_type in ("PRIVATE", "OWNER", "PRYWATNA", "PRIVATE_OWNER"):
            seller_type = "prywatna"
        elif adv_type == "DEVELOPER":
            seller_type = "deweloper"

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "source": "otodom",
        "external_id": ext_id,
        "type": prop_type,             # "działka budowlana" / "działka rolna" / …
        "dzialka_type": subtype,       # podtyp (budowlana/rolna/rekreacyjna/lesna/inwestycyjna/inne)
        "seller_type": seller_type,    # "prywatna" / "posrednik" / "deweloper"
        "title": title,
        "url": url,
        "location": location,
        "city": city_name,
        "price": price,
        "area_m2": area_m2,
        "price_pm2": round(price / area_m2, 2) if price and area_m2 and area_m2 > 0 else None,
        "lat": lat,
        "lng": lon,
        "image": img_url,
        "transaction_type": "sprzedaz",
        # added_at = prawdziwa data publikacji z Otodomu (fallback: teraz)
        "added_at": posted_iso or now_iso,
        "posted_at": posted_iso,       # oryginalna data ISO z Otodomu
        "last_seen_at": now_iso,       # kiedy ostatnio widzieliśmy ofertę
        "scraped_via": "scrapingbee",
    }


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url:
        log.error("MONGODB_URI not set")
        sys.exit(1)
    client = MongoClient(url, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    db_name = os.environ.get("MONGODB_DB") or "finderdom"
    return client[db_name]


def ensure_indexes(coll):
    try:
        coll.create_index([("source", ASCENDING), ("external_id", ASCENDING)],
                          unique=True, name="src_ext_uniq", partialFilterExpression={"source": {"$exists": True}})
    except Exception as e:
        log.debug("index create skip: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", help="Only scrape one city (slug lowercase, e.g. warszawa)")
    ap.add_argument("--max-pages", type=int, default=15,
                    help="Max pages per city (default 15 → up to 1080 listings/city)")
    args = ap.parse_args()

    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        log.error("SCRAPINGBEE_API_KEY not set")
        sys.exit(1)

    db = get_mongo()
    coll = db["listings"]
    ensure_indexes(coll)
    log.info("MongoDB: %s.listings", db.name)

    # ── One-time migration: datetime added_at → ISO string ──
    try:
        migrated = coll.update_many(
            {"scraped_via": "scrapingbee", "added_at": {"$type": "date"}},
            [{"$set": {"added_at": {"$dateToString": {
                "format": "%Y-%m-%dT%H:%M:%S.%LZ", "date": "$added_at"}}}}]
        ).modified_count
        if migrated:
            log.info("Migrated %d docs: added_at datetime → ISO string", migrated)
    except Exception as e:
        log.warning("added_at migration skipped: %s", e)

    cities = CITIES
    if args.city:
        cities = [c for c in CITIES if c["name"].lower().replace("ł", "l") == args.city.lower() or
                                        c["slug"].split("/")[0] == args.city.lower()]
        if not cities:
            log.error("Unknown city: %s", args.city)
            sys.exit(1)

    total = {"fetched": 0, "parsed": 0, "inserted": 0, "credits_used": 0}
    for city in cities:
        log.info("── %s ──", city["name"])
        for page in range(1, args.max_pages + 1):
            url = otodom_url(city, page)
            html = fetch_via_scrapingbee(url, api_key)
            total["credits_used"] += 1
            if not html:
                log.warning("  page %d: fetch failed", page)
                break
            items = parse_listings(html)
            total["fetched"] += 1
            total["parsed"] += len(items)
            if not items:
                log.info("  page %d: no listings — end of pagination", page)
                break

            docs = []
            for it in items:
                d = normalize(it, city["name"])
                if d:
                    docs.append(d)
            batch_inserted = 0
            batch_updated = 0
            if docs:
                from pymongo import UpdateOne
                ops = [UpdateOne(
                    {"source": d["source"], "external_id": d["external_id"]},
                    {"$set": d},
                    upsert=True) for d in docs]
                try:
                    result = coll.bulk_write(ops, ordered=False)
                    batch_inserted = result.upserted_count
                    batch_updated = result.modified_count
                    total["inserted"] += batch_inserted
                except Exception as e:
                    log.warning("  bulk_write error: %s", e)
            log.info("  page %d: %d items → new=%d, updated=%d (total_new=%d)",
                     page, len(items), batch_inserted, batch_updated, total["inserted"])
            # Early stop: if 0 new AND 0 updated (i.e. same 37 rec. items) for 2 pages → break
            if batch_inserted == 0 and batch_updated == 0:
                empty_streak = empty_streak + 1 if 'empty_streak' in dir() else 1
                if empty_streak >= 2:
                    log.info("  → 2 empty pages, moving to next city")
                    break
            else:
                empty_streak = 0
            time.sleep(0.5)   # gentle

    log.info("═══ DONE: pages=%d parsed=%d inserted=%d credits≈%d ═══",
             total["fetched"], total["parsed"], total["inserted"], total["credits_used"])


if __name__ == "__main__":
    main()
