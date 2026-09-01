#!/usr/bin/env python3
"""
Universal Otodom scraper via ScrapingBee — mieszkania / domy / działki.

Usage:
    export SCRAPINGBEE_API_KEY=xxx
    export MONGODB_URI="mongodb+srv://..."
    python scripts/scrape_otodom.py --property mieszkanie   # ~500 credits
    python scripts/scrape_otodom.py --property dom          # ~300 credits
    python scripts/scrape_otodom.py --property dzialka      # ~250 credits
    python scripts/scrape_otodom.py --property mieszkanie --city warszawa --max-pages 2
"""
from __future__ import annotations
import argparse, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, ASCENDING, UpdateOne

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("otodom")

SB_URL = "https://app.scrapingbee.com/api/v1/"

CITIES: List[Dict[str, str]] = [
    {"name": "Warszawa",    "voi": "mazowieckie",         "slug": "warszawa/warszawa/warszawa"},
    {"name": "Kraków",      "voi": "malopolskie",         "slug": "krakow/krakow/krakow"},
    {"name": "Wrocław",     "voi": "dolnoslaskie",        "slug": "wroclaw/wroclaw/wroclaw"},
    {"name": "Łódź",        "voi": "lodzkie",             "slug": "lodz/lodz/lodz"},
    {"name": "Poznań",      "voi": "wielkopolskie",       "slug": "poznan/poznan/poznan"},
    {"name": "Gdańsk",      "voi": "pomorskie",           "slug": "gdansk/gdansk/gdansk"},
    {"name": "Szczecin",    "voi": "zachodniopomorskie",  "slug": "szczecin/szczecin/szczecin"},
    {"name": "Bydgoszcz",   "voi": "kujawsko--pomorskie", "slug": "bydgoszcz/bydgoszcz/bydgoszcz"},
    {"name": "Lublin",      "voi": "lubelskie",           "slug": "lublin/lublin/lublin"},
    {"name": "Białystok",   "voi": "podlaskie",           "slug": "bialystok/bialystok/bialystok"},
    {"name": "Katowice",    "voi": "slaskie",             "slug": "katowice/katowice/katowice"},
    {"name": "Gdynia",      "voi": "pomorskie",           "slug": "gdynia/gdynia/gdynia"},
    {"name": "Częstochowa", "voi": "slaskie",             "slug": "czestochowa/czestochowa/czestochowa"},
    {"name": "Radom",       "voi": "mazowieckie",         "slug": "radom/radom/radom"},
    {"name": "Toruń",       "voi": "kujawsko--pomorskie", "slug": "torun/torun/torun"},
    {"name": "Rzeszów",     "voi": "podkarpackie",        "slug": "rzeszow/rzeszow/rzeszow"},
    {"name": "Kielce",      "voi": "swietokrzyskie",      "slug": "kielce/kielce/kielce"},
    {"name": "Olsztyn",     "voi": "warminsko--mazurskie","slug": "olsztyn/olsztyn/olsztyn"},
    {"name": "Zabrze",      "voi": "slaskie",             "slug": "zabrze/zabrze/zabrze"},
    {"name": "Sosnowiec",   "voi": "slaskie",             "slug": "sosnowiec/sosnowiec/sosnowiec"},
]

PROPERTY_SLUGS = {
    "mieszkanie": "mieszkanie",
    "dom":        "dom",
    "dzialka":    "dzialka",
}
PROPERTY_LABELS = {
    "mieszkanie": "mieszkanie",
    "dom":        "dom",
    "dzialka":    "działka",
}


def otodom_url(prop: str, city: Dict[str, str], page: int) -> str:
    slug = PROPERTY_SLUGS[prop]
    return f"https://www.otodom.pl/pl/wyniki/sprzedaz/{slug}/{city['voi']}/{city['slug']}?limit=72&page={page}"


def fetch_via_scrapingbee(url: str, api_key: str) -> Optional[str]:
    params = {"api_key": api_key, "url": url, "render_js": "false",
              "premium_proxy": "false", "country_code": "pl"}
    try:
        r = requests.get(SB_URL, params=params, timeout=60)
        if r.status_code == 200:
            return r.text
        log.warning("SB HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("SB fetch error: %s", e)
    return None


NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def parse_listings(html: str) -> List[Dict[str, Any]]:
    m = NEXT_DATA_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    def walk(node):
        if isinstance(node, dict):
            if "items" in node and isinstance(node["items"], list) and node["items"]:
                first = node["items"][0]
                if isinstance(first, dict) and ("id" in first or "slug" in first):
                    return node["items"]
            for v in node.values():
                r = walk(v)
                if r: return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r: return r
        return None
    return [x for x in (walk(data) or []) if isinstance(x, dict)]


def _num(v) -> Optional[float]:
    if v is None: return None
    try: return float(str(v).replace(",", ".").replace(" ", ""))
    except: return None


DZIALKA_SUBTYPES = [
    ("budowlana",    ["budowlan"]),
    ("rolna",        ["rolna", "rolne", "rolny"]),
    ("rekreacyjna",  ["rekreacyj"]),
    ("siedliskowa",  ["siedlisk"]),
    ("lesna",        ["leśn", "lesn"]),
    ("inwestycyjna", ["usługow", "uslugow", "komercyj", "inwestycyj"]),
]
DOM_SUBTYPES = [
    ("wolnostojący", ["wolnostoj"]),
    ("bliźniak",     ["bliźn", "blizn"]),
    ("szeregowiec",  ["szereg"]),
    ("kamienica",    ["kamienic"]),
]


def normalize(item: Dict[str, Any], city_name: str, prop: str) -> Optional[Dict[str, Any]]:
    slug = item.get("slug") or item.get("url")
    ext_id = str(item.get("id") or item.get("advertId") or slug or "")
    if not ext_id or not slug: return None
    url = f"https://www.otodom.pl/pl/oferta/{slug}" if not slug.startswith("http") else slug

    title = item.get("title") or PROPERTY_LABELS[prop].capitalize()
    text_blob = f"{title} {item.get('shortDescription','')} {item.get('description','')}".lower()

    # ── Prawdziwa data publikacji z Otodomu ──
    # Otodom podaje: "dateCreated" (YYYY-MM-DD HH:MM:SS) i "createdAtFirst" (ISO).
    # Preferujemy pierwsze wystawienie ("createdAtFirst"), fallback do "dateCreated".
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

    # ── Seller type detection ──
    seller_type = "posrednik"
    if item.get("isPrivateOwner"):
        seller_type = "prywatna"
    elif item.get("isDeveloperOwner"):
        seller_type = "deweloper"
    else:
        adv_type = (item.get("extendedAdvertiserType") or item.get("advertiserType") or "").upper()
        if adv_type == "PRIVATE":
            seller_type = "prywatna"
        elif adv_type == "DEVELOPER":
            seller_type = "deweloper"

    # Subtype detection
    subtype = None
    subtype_field = None
    if prop == "dzialka":
        subtype_source = str(item.get("dzialkaType") or item.get("estateType") or "").lower()
        # Najpierw próba po polu z Otodomu, potem ZAWSZE fallback do tekstu (title+desc)
        for label, kws in DZIALKA_SUBTYPES:
            if any(kw in subtype_source for kw in kws):
                subtype = label; break
        if not subtype:
            for label, kws in DZIALKA_SUBTYPES:
                if any(kw in text_blob for kw in kws):
                    subtype = label; break
        subtype_field = "dzialka_type"
        base = "działka"
    elif prop == "dom":
        subtype_source = str(item.get("buildingType") or item.get("houseType") or "").lower()
        for label, kws in DOM_SUBTYPES:
            if any(kw in subtype_source for kw in kws):
                subtype = label; break
        if not subtype:
            for label, kws in DOM_SUBTYPES:
                if any(kw in text_blob for kw in kws):
                    subtype = label; break
        subtype_field = "building_type"
        base = "dom"
    else:  # mieszkanie
        base = "mieszkanie"

    prop_type = f"{base} {subtype}" if subtype else base

    # price + area
    price = None
    tp = item.get("totalPrice") or {}
    if isinstance(tp, dict): price = _num(tp.get("value"))
    if not price: price = _num(item.get("price"))
    area_m2 = _num(item.get("areaInSquareMeters")) or _num(item.get("area"))

    # rooms (for mieszkania/domy)
    rooms = _num(item.get("roomsNumber"))

    # location
    loc = item.get("location") or {}
    location = ""
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            city_part = (addr.get("city") or {}).get("name", "") if isinstance(addr.get("city"), dict) else ""
            district = (addr.get("district") or {}).get("name", "") if isinstance(addr.get("district"), dict) else ""
            location = ", ".join(x for x in [city_part, district] if x)
    if not location: location = city_name

    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    lat = _num((coords or {}).get("latitude"))
    lon = _num((coords or {}).get("longitude"))

    images = item.get("images") or []
    img_url = None
    if images and isinstance(images[0], dict):
        img_url = images[0].get("large") or images[0].get("medium") or images[0].get("small")

    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "source": "otodom",
        "external_id": ext_id,
        "type": prop_type,
        "title": title,
        "url": url,
        "location": location,
        "city": city_name,
        "price": price,
        "area_m2": area_m2,
        "price_pm2": round(price / area_m2, 2) if price and area_m2 and area_m2 > 0 else None,
        "rooms": int(rooms) if rooms else None,
        "lat": lat,
        "lng": lon,
        "image": img_url,
        "transaction_type": "sprzedaz",
        "seller_type": seller_type,
        # added_at = prawdziwa data publikacji na Otodomie (fallback: teraz jeśli nie ma daty)
        "added_at": posted_iso or now_iso,
        "posted_at": posted_iso,          # oryginalna data ISO z Otodomu (jeśli była)
        "last_seen_at": now_iso,          # kiedy ostatnio widzieliśmy ofertę (live)
        "scraped_via": "scrapingbee",
    }
    if subtype_field and subtype:
        doc[subtype_field] = subtype
    return doc


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url: log.error("MONGODB_URI not set"); sys.exit(1)
    client = MongoClient(url, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    return client[os.environ.get("MONGODB_DB") or "finderdom"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", choices=list(PROPERTY_SLUGS.keys()), required=True)
    ap.add_argument("--city", help="One city slug (lowercase)")
    ap.add_argument("--max-pages", type=int, default=15)
    args = ap.parse_args()

    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key: log.error("SCRAPINGBEE_API_KEY not set"); sys.exit(1)

    db = get_mongo()
    coll = db["listings"]

    # ── One-time migration: convert datetime added_at → ISO string ──
    # (needed because frontend filters expect string date)
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
        cities = [c for c in CITIES if c["slug"].split("/")[0] == args.city.lower()]
        if not cities: log.error("Unknown city: %s", args.city); sys.exit(1)

    log.info("═══ Otodom scraper — property=%s cities=%d max_pages=%d ═══",
             args.property, len(cities), args.max_pages)
    total = {"new": 0, "updated": 0, "credits": 0}

    for city in cities:
        log.info("── %s ──", city["name"])
        empty_streak = 0
        for page in range(1, args.max_pages + 1):
            url = otodom_url(args.property, city, page)
            html = fetch_via_scrapingbee(url, api_key)
            total["credits"] += 1
            if not html:
                log.warning("  page %d: fetch failed", page); break
            items = parse_listings(html)
            if not items:
                log.info("  page %d: no listings", page); break

            docs = [d for d in (normalize(it, city["name"], args.property) for it in items) if d]
            batch_new = batch_upd = 0
            if docs:
                # added_at = zawsze prawdziwa data z Otodomu (posted_at) — pozwala poprawnie działać
                # filtrom "dziś / 3 dni / 7 dni" nawet po wielokrotnych scrape'ach.
                ops = [UpdateOne({"source": d["source"], "external_id": d["external_id"]},
                                 {"$set": d}, upsert=True) for d in docs]
                try:
                    r = coll.bulk_write(ops, ordered=False)
                    batch_new = r.upserted_count
                    batch_upd = r.modified_count
                    total["new"] += batch_new
                    total["updated"] += batch_upd
                except Exception as e:
                    log.warning("  bulk_write: %s", e)
            log.info("  page %d: %d items → new=%d, updated=%d (total_new=%d)",
                     page, len(items), batch_new, batch_upd, total["new"])
            if batch_new == 0 and batch_upd == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    log.info("  → 2 empty pages, next city"); break
            else:
                empty_streak = 0
            time.sleep(0.5)

    log.info("═══ DONE property=%s: new=%d updated=%d credits≈%d ═══",
             args.property, total["new"], total["updated"], total["credits"])


if __name__ == "__main__":
    main()
