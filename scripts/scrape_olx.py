#!/usr/bin/env python3
"""
OLX.pl → MongoDB `listings` — bezpośrednio z GitHub Actions (0 zł), bez Apify.

Strony list OLX zawierają pełne dane ogłoszeń w `window.__PRERENDERED_STATE__` (JSON): tytuł, cena, m², pokoje,
zdjęcia, lokalizacja, współrzędne, data dodania, czy prywatne. 1 zapytanie = ~40 ofert. Sort: od najnowszych.
Alternatywnie publiczne API JSON (www.olx.pl/api/v1/offers/) — używane jako fallback.

Użycie:
  python scripts/scrape_olx.py --property mieszkanie --transaction sprzedaz [--city krakow] [--max-pages 5]
"""
import argparse, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_otodom import CITIES, DOM_SUBTYPES, DZIALKA_SUBTYPES, _num  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("olx")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

SEG = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki"}
BASE_LABEL = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "działka"}
ROOMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def list_url(prop: str, trans: str, city_slug: str, page: int) -> str:
    u = f"https://www.olx.pl/nieruchomosci/{SEG[prop]}/{trans}/{city_slug}/?search%5Border%5D=created_at%3Adesc"
    return u + (f"&page={page}" if page > 1 else "")


def fetch(url: str) -> Optional[str]:
    for _ in range(3):
        try:
            r = S.get(url, timeout=40)
            if r.status_code == 200 and len(r.text) > 20000: return r.text
            if r.status_code in (403, 429): log.warning("  HTTP %s — pauza 30s", r.status_code); time.sleep(30)
            elif r.status_code == 404: return None
            else: log.warning("  HTTP %s", r.status_code)
        except requests.RequestException as e:
            log.warning("  fetch error: %s", str(e)[:100]); time.sleep(3)
    return None


def _find_ads(node) -> Optional[List[dict]]:
    """Szukamy w stanie listy obiektów wyglądających jak ogłoszenia (mają 'id' i 'url' z olx.pl/d/oferta)."""
    if isinstance(node, dict):
        if "ads" in node and isinstance(node["ads"], list) and node["ads"] and isinstance(node["ads"][0], dict) and "id" in node["ads"][0]:
            return node["ads"]
        for v in node.values():
            r = _find_ads(v)
            if r: return r
    elif isinstance(node, list):
        for v in node:
            r = _find_ads(v)
            if r: return r
    return None


def parse_list(html: str) -> List[dict]:
    m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*(".*?");\s*\n', html, re.S) or \
        re.search(r'window\.__PRERENDERED_STATE__\s*=\s*(".*?");', html, re.S)
    if not m:
        return []
    try:
        state = json.loads(json.loads(m.group(1)))
    except Exception as e:
        log.warning("  state parse: %s", str(e)[:80]); return []
    return _find_ads(state) or []


def _param(ad: dict, key: str):
    for p in ad.get("params") or []:
        if p.get("key") == key:
            return p.get("value")
    return None


def normalize(ad: dict, city_name: str, prop: str, trans: str) -> Optional[Dict[str, Any]]:
    oid = str(ad.get("id") or "")
    url = ad.get("url") or ""
    if not oid or not url: return None
    title = ad.get("title") or ""
    desc = (ad.get("description") or "")[:3000]
    pv = _param(ad, "price") or {}
    price = _num(pv.get("value")) if isinstance(pv, dict) else _num(pv)
    mv = _param(ad, "m") or {}
    area = _num((mv.get("key") if isinstance(mv, dict) else mv) or "") or _num(re.sub(r"[^\d.,]", "", str((mv.get("label") if isinstance(mv, dict) else "") or "")).replace(",", "."))
    rv = _param(ad, "rooms") or {}
    rooms = ROOMS.get((rv.get("key") if isinstance(rv, dict) else rv) or "") or _num(re.sub(r"\D", "", str((rv.get("label") if isinstance(rv, dict) else "") or "")))
    mk = _param(ad, "market") or {}
    market = {"primary": "pierwotny", "secondary": "wtorny"}.get((mk.get("key") if isinstance(mk, dict) else mk) or "")
    fl = _param(ad, "floor_select") or {}
    floor = (fl.get("label") if isinstance(fl, dict) else fl) or None
    photos = []
    for ph in ad.get("photos") or []:
        link = ph.get("link") if isinstance(ph, dict) else ph
        if isinstance(link, str): photos.append(link.replace("{width}x{height}", "1080x720"))
    loc = ad.get("location") or {}
    city = loc.get("cityName") or city_name
    district = loc.get("districtName")
    mp = ad.get("map") or {}
    lat, lng = _num(mp.get("lat")), _num(mp.get("lon"))
    created = ad.get("createdTime") or ad.get("created_time") or ad.get("lastRefreshTime")
    posted = None
    if created:
        try: posted = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError: posted = None
    blob = f"{title} {desc}".lower()
    subtype = None
    if prop == "dzialka":
        for label, kws in DZIALKA_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    elif prop == "dom":
        for label, kws in DOM_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    ptype = f"{BASE_LABEL[prop]} {subtype}" if subtype else BASE_LABEL[prop]
    is_business = ad.get("isBusiness") if "isBusiness" in ad else ad.get("business")
    seller = "posrednik" if is_business else "prywatna"
    if is_business and re.search(r"dewelop", blob): seller = "deweloper"
    now_iso = datetime.now(timezone.utc).isoformat()
    doc: Dict[str, Any] = {
        "source": "olx", "external_id": oid, "type": ptype, "title": title or ptype.capitalize(), "url": url,
        "city": city, "district": district, "location": ", ".join(x for x in [city, district] if x),
        "price": price, "area_m2": area, "price_pm2": round(price / area, 2) if price and area and area > 0 else None,
        "rooms": int(rooms) if rooms else None, "floor": floor, "market_type": market,
        "image": photos[0] if photos else None, "images": photos[:12], "description": desc,
        "lat": lat, "lng": lng, "transaction_type": trans, "seller_type": seller,
        "posted_at": posted, "last_seen_at": now_iso, "scraped_via": "olx-direct",
    }
    if subtype: doc["dzialka_type" if prop == "dzialka" else "building_type"] = subtype
    return doc


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url: log.error("MONGODB_URI not set"); sys.exit(1)
    c = MongoClient(url, serverSelectionTimeoutMS=8000); c.admin.command("ping")
    return c[os.environ.get("MONGODB_DB") or "finderdom"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", choices=list(SEG), required=True)
    ap.add_argument("--transaction", choices=["sprzedaz", "wynajem"], default="sprzedaz")
    ap.add_argument("--city"); ap.add_argument("--max-pages", type=int, default=5); ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    db = get_mongo(); coll = db["listings"]
    cities = CITIES if not a.city else [c for c in CITIES if c["slug"].split("/")[0] == a.city.lower()]
    if not cities: log.error("Unknown city"); sys.exit(1)
    log.info("═══ OLX direct — %s/%s — cities=%d max_pages=%d ═══", a.transaction, a.property, len(cities), a.max_pages)
    tot = {"new": 0, "updated": 0, "pages": 0}
    for city in cities:
        slug = city["slug"].split("/")[0]
        log.info("── %s ──", city["name"])
        for page in range(1, a.max_pages + 1):
            html = fetch(list_url(a.property, a.transaction, slug, page)); tot["pages"] += 1
            if not html: log.info("  page %d: brak HTML", page); break
            ads = parse_list(html)
            if not ads:
                log.info("  page %d: 0 ogłoszeń (PRERENDERED_STATE: %s)", page, "jest" if "__PRERENDERED_STATE__" in html else "brak"); break
            if a.debug and page == 1:
                log.info("  DEBUG keys: %s", sorted(ads[0].keys())[:40])
                log.info("  DEBUG params: %s", [p.get("key") for p in ads[0].get("params") or []])
            ops = []
            for ad in ads:
                d = normalize(ad, city["name"], a.property, a.transaction)
                if not d or not d["price"]: continue
                ops.append(UpdateOne({"source": "olx", "external_id": d["external_id"]},
                                     {"$set": d, "$setOnInsert": {"added_at": d["posted_at"] or d["last_seen_at"]},
                                      "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}}, upsert=True))
            new = upd = 0
            if ops:
                try:
                    r = coll.bulk_write(ops, ordered=False); new, upd = r.upserted_count, r.modified_count
                except Exception as e:
                    log.warning("  bulk_write: %s", str(e)[:200])
            tot["new"] += new; tot["updated"] += upd
            log.info("  page %d: %d ogłoszeń → new=%d upd=%d", page, len(ads), new, upd)
            if new == 0 and page >= 2: log.info("  → brak nowych, następne miasto"); break
            time.sleep(a.delay)
    log.info("═══ DONE olx %s/%s: new=%d updated=%d pages=%d ═══", a.transaction, a.property, tot["new"], tot["updated"], tot["pages"])


if __name__ == "__main__":
    main()
