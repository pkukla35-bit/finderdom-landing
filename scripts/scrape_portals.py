#!/usr/bin/env python3
"""
Darmowy scraper (bez proxy, 0 zł) dla portali: morizon, gratka, domiporta, adresowo → MongoDB `listings`.
Strony list są renderowane serwerowo; pobieramy tylko listy (bez szczegółów), więc 1 zapytanie = 35–40 ofert.

Użycie:
  python scripts/scrape_portals.py --portal morizon --property mieszkanie --transaction sprzedaz [--city krakow] [--max-pages 5]
Env: MONGODB_URI (lub MONGO_URL), MONGODB_DB
"""
import argparse, html as htmllib, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_otodom import CITIES, DOM_SUBTYPES, DZIALKA_SUBTYPES, _num  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("portals")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,*/*;q=0.8"})
BASE_LABEL = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "działka"}


def city_slug(city: Dict[str, str]) -> str:
    return city["slug"].split("/")[0]


# ───────────────────────── URL builders ─────────────────────────
def url_morizon(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki"}[prop]
    base = f"https://www.morizon.pl/{'do-wynajecia/' if trans == 'wynajem' else ''}{seg}/{city_slug(city)}/"
    return base + (f"?page={page}" if page > 1 else "")


def url_gratka(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki-grunty"}[prop]
    base = f"https://gratka.pl/nieruchomosci/{seg}/{city_slug(city)}" + ("/wynajem" if trans == "wynajem" else "")
    return base + (f"?page={page}" if page > 1 else "")


def url_domiporta(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "dzialke"}[prop]
    t = "wynajme" if trans == "wynajem" else "sprzedam"
    base = f"https://www.domiporta.pl/{seg}/{t}/{city['voi']}/{city_slug(city)}"
    return base + (f"?PageNumber={page}" if page > 1 else "")


def url_adresowo(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki"}[prop]
    if trans == "wynajem": seg += "-wynajem"
    base = f"https://adresowo.pl/{seg}/{city_slug(city)}/"
    return base + (f"_l{page}" if page > 1 else "")


# ───────────────────────── Parsers (→ lista słowników: id,url,title,price,area,rooms,image,district,street,desc,floor,seller) ─────────────────────────
def _txt(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or ""); s = htmllib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _money(s: str) -> Optional[float]:
    s = htmllib.unescape(s or "").replace("\xa0", "").replace(" ", "")
    m = re.search(r"(\d[\d.]*)", s.replace(",", "."))
    return _num(m.group(1)) if m else None


def parse_ldjson_offers(html: str, id_re: str) -> List[Dict[str, Any]]:
    """Morizon + Gratka (ta sama grupa): pełna lista ofert w <script type=application/ld+json> @type=Product."""
    out = []
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try: d = json.loads(m)
        except Exception: continue
        if d.get("@type") != "Product": continue
        for o in (d.get("offers") or {}).get("offers") or []:
            url = o.get("url") or ""
            mid = re.search(id_re, url)
            if not mid: continue
            it = o.get("itemOffered") or {}
            addr = it.get("address") or {}
            fs = it.get("floorSize") or {}
            out.append({
                "id": mid.group(1), "url": url, "title": o.get("name") or "",
                "price": _num(o.get("price")), "area": _num(fs.get("value")) if isinstance(fs, dict) else None,
                "rooms": _num(it.get("numberOfRooms")), "floor": it.get("floorLevel"),
                "image": o.get("image"), "district": addr.get("addressLocality"), "street": addr.get("streetAddress"),
                "desc": it.get("description") or "", "seller": None,
            })
        break
    return out


def parse_morizon(html): return parse_ldjson_offers(html, r"-mzn(\d+)$")
def parse_gratka(html): return parse_ldjson_offers(html, r"/ob/(\d+)")


def parse_domiporta(html: str) -> List[Dict[str, Any]]:
    out = []
    for a in re.findall(r'<article class="sneakpeak[^"]*"(.*?)</article>', html, re.S):
        mid = re.search(r'data-detail-id="(\d+)"', a)
        href = re.search(r'href="(/nieruchomosci/[^"]+/\d+)"', a)
        if not mid or not href: continue
        title = re.search(r'<h2 class="sneakpeak__title--bold">(.*?)</h2>', a, re.S)
        loc = re.search(r'<span class="sneakpeak__title--inblock">(.*?)</span>', a, re.S)
        price = re.search(r'<span class="sneakpeak__price_value">(.*?)</span>', a, re.S)
        area = re.search(r'sneakpeak__details_item--area"[^>]*>(.*?)</span>', a, re.S)
        rooms = re.search(r'sneakpeak__details_item--room"[^>]*>(.*?)</span>', a, re.S)
        img = re.search(r'<img[^>]+src="(https://galeria\.domiporta\.pl/[^"]+)"', a)
        desc = re.search(r'<p class="sneakpeak__[a-z_]*"[^>]*>([^<]{5,})', a)
        loc_parts = [p.strip() for p in _txt(loc.group(1)).split(",")] if loc else []
        out.append({
            "id": mid.group(1), "url": "https://www.domiporta.pl" + href.group(1), "title": _txt(title.group(1)) if title else "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": _num(_txt(rooms.group(1))) if rooms else None, "floor": None, "image": img.group(1) if img else None,
            "district": loc_parts[1] if len(loc_parts) > 1 else None, "street": loc_parts[2] if len(loc_parts) > 2 else None,
            "desc": _txt(desc.group(1)) if desc else "", "seller": None,
        })
    return out


def parse_adresowo(html: str) -> List[Dict[str, Any]]:
    out = []
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    cards = re.split(r'<div data-offer-card data-id="', html)[1:]
    for c in cards:
        mid = re.match(r"(\d+)", c)
        href = re.search(r'href="(/o/[^"]+)"', c)
        if not mid or not href: continue
        img = re.search(r'srcset="(https://[^" ]+\.(?:webp|jpg))', c)
        nums = re.findall(r'<span class="font-bold">([\d\s\xa0&nbsp;,.]+)</span>\s*<span class="text-xs text-neutral-700">\s*(zł|m²|pok\.)', c)
        price = area = rooms = None
        for v, unit in nums:
            if unit == "zł": price = _money(v)
            elif unit == "m²": area = _money(v)
            else: rooms = _money(v)
        lines = re.findall(r'<span class="line-clamp-1[^"]*">([^<]*)</span>', c)
        desc = re.search(r'<p class="mb-3 line-clamp-4[^"]*">\s*([^<]+)', c)
        loc = _txt(lines[0]) if lines else ""
        street = _txt(lines[1]) if len(lines) > 1 else None
        district = loc.split(" ", 1)[1] if " " in loc else None
        out.append({
            "id": mid.group(1), "url": "https://adresowo.pl" + href.group(1),
            "title": " ".join(x for x in [loc, street] if x), "price": price, "area": area, "rooms": rooms, "floor": None,
            "image": img.group(1) if img else None, "district": district, "street": street,
            "desc": _txt(desc.group(1)) if desc else "", "seller": "prywatna",   # adresowo = tylko bez pośredników
        })
    return out


PORTALS = {
    "morizon":   {"url": url_morizon,   "parse": parse_morizon},
    "gratka":    {"url": url_gratka,    "parse": parse_gratka},
    "domiporta": {"url": url_domiporta, "parse": parse_domiporta},
    "adresowo":  {"url": url_adresowo,  "parse": parse_adresowo},
}


def fetch(url: str) -> Optional[str]:
    for _ in range(3):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 3000: return r.text
            if r.status_code == 404: return None
            if r.status_code in (403, 429): log.warning("  HTTP %s — pauza 20s", r.status_code); time.sleep(20)
        except requests.RequestException as e:
            log.warning("  fetch error: %s", str(e)[:100]); time.sleep(3)
    return None


def normalize(portal: str, it: Dict[str, Any], city_name: str, prop: str, trans: str) -> Dict[str, Any]:
    blob = f"{it['title']} {it.get('desc', '')}".lower()
    subtype = None
    if prop == "dzialka":
        for label, kws in DZIALKA_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    elif prop == "dom":
        for label, kws in DOM_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    ptype = f"{BASE_LABEL[prop]} {subtype}" if subtype else BASE_LABEL[prop]
    seller = it.get("seller") or ("prywatna" if re.search(r"bez pośredni|od właściciela|sprzedam bezpośrednio|oferta prywatna", blob) else
                                  "deweloper" if re.search(r"dewelop|inwestycja deweloperska", blob) else "posrednik")
    price, area = it.get("price"), it.get("area")
    now_iso = datetime.now(timezone.utc).isoformat()
    doc: Dict[str, Any] = {
        "source": portal, "external_id": str(it["id"]), "type": ptype, "title": it["title"] or ptype.capitalize(),
        "url": it["url"], "city": city_name, "district": it.get("district"),
        "location": ", ".join(x for x in [city_name, it.get("district"), it.get("street")] if x),
        "price": price, "area_m2": area, "price_pm2": round(price / area, 2) if price and area and area > 0 else None,
        "rooms": int(it["rooms"]) if it.get("rooms") else None, "floor": it.get("floor"),
        "image": it.get("image"), "images": [it["image"]] if it.get("image") else [],
        "description": (it.get("desc") or "")[:2000], "transaction_type": trans, "seller_type": seller,
        "last_seen_at": now_iso, "scraped_via": "portal-direct",
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
    ap.add_argument("--portal", choices=list(PORTALS), required=True)
    ap.add_argument("--property", choices=list(BASE_LABEL), required=True)
    ap.add_argument("--transaction", choices=["sprzedaz", "wynajem"], default="sprzedaz")
    ap.add_argument("--city"); ap.add_argument("--max-pages", type=int, default=5); ap.add_argument("--delay", type=float, default=1.0)
    a = ap.parse_args()
    P = PORTALS[a.portal]
    db = get_mongo(); coll = db["listings"]
    cities = CITIES if not a.city else [c for c in CITIES if city_slug(c) == a.city.lower()]
    if not cities: log.error("Unknown city"); sys.exit(1)
    log.info("═══ %s — %s/%s — cities=%d max_pages=%d ═══", a.portal, a.transaction, a.property, len(cities), a.max_pages)
    tot = {"new": 0, "updated": 0, "pages": 0}
    for city in cities:
        log.info("── %s ──", city["name"])
        for page in range(1, a.max_pages + 1):
            html = fetch(P["url"](a.property, a.transaction, city, page)); tot["pages"] += 1
            if not html: log.info("  page %d: brak HTML", page); break
            items = P["parse"](html)
            if not items: log.info("  page %d: 0 ofert", page); break
            ops = []
            for it in items:
                d = normalize(a.portal, it, city["name"], a.property, a.transaction)
                if not d["price"]: continue
                ops.append(UpdateOne({"source": a.portal, "external_id": d["external_id"]},
                                     {"$set": d, "$setOnInsert": {"added_at": d["last_seen_at"], "posted_at": None},
                                      "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}}, upsert=True))
            new = upd = 0
            if ops:
                try:
                    r = coll.bulk_write(ops, ordered=False); new, upd = r.upserted_count, r.modified_count
                except Exception as e:
                    log.warning("  bulk_write: %s", str(e)[:200])
            tot["new"] += new; tot["updated"] += upd
            log.info("  page %d: %d ofert → new=%d upd=%d", page, len(items), new, upd)
            if new == 0 and page >= 2: log.info("  → brak nowych, następne miasto"); break
            time.sleep(a.delay)
    log.info("═══ DONE %s %s/%s: new=%d updated=%d pages=%d ═══", a.portal, a.transaction, a.property, tot["new"], tot["updated"], tot["pages"])


if __name__ == "__main__":
    main()
