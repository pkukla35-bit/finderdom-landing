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
    return " · ".join(parts) + " — " + (f"{city}, {cname}" if city else cname)


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
        city = loc[0] if loc and loc[0] != cname else None     # route „Hiszpania” bez miasta → brak miasta
        region = loc[1] if len(loc) > 2 else None
        price_pln = _price(pln.group(1)) if pln else None
        area_v = _money(area.group(1)) if area else None
        typ = typ_default or _type_from_title(t)
        if trans == "sprzedaz" and typ_default is None: typ = _type_from_title(t)
        out.append({
            "source": "realting", "external_id": f"rt-{oid}", "url": url.group(1),
            "title": _title_pl(t, area_v, city or "", cname), "title_orig": t,
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


# ───────────────────────── kursy NBP (EUR/USD → PLN) ─────────────────────────
_RATES: Dict[str, float] = {}


def rate(code: str) -> float:
    if code == "PLN": return 1.0
    if code not in _RATES:
        try:
            import json, urllib.request
            with urllib.request.urlopen(f"https://api.nbp.pl/api/exchangerates/rates/a/{code.lower()}/?format=json", timeout=10) as r:
                _RATES[code] = float(json.loads(r.read())["rates"][0]["mid"])
        except Exception as e:
            log.warning("NBP %s: %s", code, str(e)[:80]); _RATES[code] = {"EUR": 4.45, "USD": 3.85, "GBP": 5.1}.get(code, 4.0)
    return _RATES[code]


# ───────────────────────── green-acres (.es/.it/.pt — sprzedaż, 24 karty/str., do 20 zdjęć, sort=date) ─────────────────────────
GA_DOMAINS = {"es": "spain", "it": "italy", "pt": "portugal"}
GA_TYPE = [("dzialka", r"działk|grunt|teren|ziemi"), ("lokal", r"lokal|biur|sklep|hotel|magazyn|komercyj|restaurac|budynek|kamienic"),
           ("dom", r"dom|willa|szereg|bungalow|chalet|finca|cortijo|masseria|trullo|rustic|gospodarstw|rezydenc|zamek|casa"),
           ("mieszkanie", r"mieszkan|apartament|kawalerk|penthouse|studio|loft|duplex|attyk")]


def parse_greenacres(html: str, dom: str) -> List[Dict[str, Any]]:
    import base64
    iso, cname, flag = COUNTRIES[GA_DOMAINS[dom]]
    out, seen = [], set()
    for m in re.finditer(r'<div class="announce-card[^"]*"[^>]*data-advertid="([^"]+)" data-o="([^"]+)"(.*?)<div class="footer-line">', html, re.S):
        aid, o, c = m.group(1), m.group(2), m.group(3)
        if aid in seen: continue
        seen.add(aid)
        try: url = base64.b64decode(o + "=" * (-len(o) % 4)).decode()
        except Exception: continue
        if "properties/" not in url: continue
        imgs = re.findall(r'(https://lb1\.green-acres\.com/[^"\s?]+/miniPhotos/[^"\s?]+\.jpg)', c)
        imgs = list(dict.fromkeys(imgs))
        title = re.search(r'<div class="announce-info" title="([^"]*)"', c)
        price = re.search(r'<strong class="info-price"[^>]*>([^<]*)</strong>', c)
        loc = re.search(r'<div class="announce-localisation">\s*([^<]*?)\s*</div>', c)
        tags = re.findall(r'<div class="info-tag shown" title="([^"]*)">([^<]*)</div>', c)
        desc = re.search(r'<div class="description-details">\s*(.*?)\s*</div>', c, re.S)
        t = _txt(title.group(1)) if title else ""          # „Zakup: Dom Rojales”
        kind = next((v for k, v in tags if "Typ" in k), "")
        rooms = next((int(re.search(r"\d+", v).group()) for k, v in tags if k == "Pokoje" and re.search(r"\d+", v)), None)
        area = next((_money(v) for k, v in tags if "Powierzchnia" in k), None)
        plot = next((_money(v) for k, v in tags if "Teren" in k), None)
        lt = _txt(loc.group(1)) if loc else ""
        mloc = re.match(r"(.*?)\s*\((.*?)\)\s*$", lt)
        city, region = (mloc.group(1).strip(), mloc.group(2).strip()) if mloc else (lt or None, None)
        pe = _price((price.group(1) or "").replace("\xa0", " ").replace("&#xA0;", " ")) if price else None
        ppln = round(pe * rate("EUR")) if pe else None
        typ = next((ty for ty, rx in GA_TYPE if re.search(rx, (kind + " " + t).lower())), "dom")
        label = kind or t.split(":", 1)[-1].strip()
        out.append({
            "source": "greenacres", "external_id": f"ga-{aid}", "url": url,
            "title": " · ".join(x for x in [label, f"{rooms} pok." if rooms else "", f"{area:g} m²" if area else ""] if x) + " — " + (f"{city}, {cname}" if city else cname),
            "title_orig": t, "type": typ, "transaction_type": "sprzedaz",
            "price": ppln, "price_pln": ppln, "price_eur": pe, "price_usd": None, "currency_orig": "EUR",
            "price_pm2": round(ppln / area) if ppln and area and area > 5 else None,
            "area_m2": area, "plot_area_m2": plot, "rooms": rooms, "bedrooms": None, "bathrooms": None, "floor": None,
            "city": city, "region": region, "country": iso, "country_name": cname, "country_flag": flag, "location": lt,
            "image": imgs[0] if imgs else None, "images": imgs[:20],
            "description": _txt(desc.group(1)).replace("...", "").strip() if desc else "",
            "seller_type": "posrednik", "agency_name": None,
            "is_stale": False, "last_seen_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


def run_greenacres(pages: int = 2, coll=None, delay: float = 1.5) -> Dict[str, int]:
    coll = coll if coll is not None else get_mongo()["listings_foreign"]
    tot = {"new": 0, "updated": 0, "pages": 0}
    for dom in GA_DOMAINS:
        zero = 0
        for page in range(1, pages + 1):
            html = fetch(f"https://www.green-acres.{dom}/pl/show_properties.htm?sort=date&page={page}")
            if not html: break
            items = parse_greenacres(html, dom); tot["pages"] += 1
            if not items: break
            now = datetime.now(timezone.utc).isoformat()
            r = coll.bulk_write([UpdateOne({"source": "greenacres", "external_id": d["external_id"]},
                                           {"$set": d, "$setOnInsert": {"added_at": now, "scraped_via": "zagranica"}}, upsert=True) for d in items], ordered=False)
            tot["new"] += r.upserted_count; tot["updated"] += r.modified_count
            log.info("  green-acres.%s p%d: %d kart → new=%d upd=%d", dom, page, len(items), r.upserted_count, r.modified_count)
            zero = zero + 1 if r.upserted_count == 0 else 0
            if zero >= 2 and page >= 2: break
            time.sleep(delay)
    log.info("═══ DONE green-acres: new=%d updated=%d pages=%d ═══", tot["new"], tot["updated"], tot["pages"])
    return tot


# ───────────────────────── landelity.com (oferty bezpośrednio od sprzedających, cały świat; sitemap + JSON-LD) ─────────────────────────
LD_COUNTRY = {"Spain": "spain", "Italy": "italy", "United States": "united-states", "USA": "united-states", "Portugal": "portugal"}


def run_landelity(limit: int = 150, coll=None, delay: float = 1.0) -> Dict[str, int]:
    """Sitemap (≈5,5 tys. ogłoszeń, lastmod) → najnowsze nieznane → strona /pl/ad-… → JSON-LD RealEstateListing + Offer.
    Zapisujemy tylko kraje z COUNTRIES. limit stron/run, żeby nie zarzynać małego serwisu."""
    import json
    coll = coll if coll is not None else get_mongo()["listings_foreign"]
    sm = fetch("https://landelity.com/sitemap.xml") or ""
    urls = re.findall(r"<loc>(https://landelity\.com/en/ad-(\d+)-\d+/[^<]+)</loc>\s*<lastmod>([^<]*)</lastmod>", sm)
    urls.sort(key=lambda x: x[2], reverse=True)
    known = {d["external_id"] for d in coll.find({"source": "landelity"}, {"external_id": 1})}
    tot = {"new": 0, "updated": 0, "pages": 0, "skipped_country": 0}
    for url, aid, lastmod in urls:
        ext = f"ld-{aid}"
        if ext in known and tot["pages"] > 0: continue
        if tot["pages"] >= limit: break
        if ext in known: continue
        html = fetch(url.replace("/en/", "/pl/")) or fetch(url); tot["pages"] += 1; time.sleep(delay)
        if not html: continue
        m = re.search(r'<script type="application/ld(?:\+|&#x2B;)json">\s*(\{.*?"RealEstateListing".*?\})\s*</script>', html, re.S)
        if not m: continue
        try: d = json.loads(m.group(1))
        except Exception: continue
        addr = d.get("address") or {}; off = d.get("offers") or {}
        cslug = LD_COUNTRY.get((addr.get("addressCountry") or "").strip())
        if not cslug: tot["skipped_country"] += 1; continue
        iso, cname, flag = COUNTRIES[cslug]
        name = d.get("name") or ""
        cur = (off.get("priceCurrency") or "USD").upper(); p = off.get("price")
        try: p = float(p)
        except (TypeError, ValueError): p = None
        ppln = round(p * rate(cur)) if p else None
        area = re.search(r"([\d.,]+)\s*(m²|m2|Ar|ha)\b", name)
        area_v = None
        if area:
            v = _money(area.group(1)); u = area.group(2)
            area_v = v * 100 if u == "Ar" and v else v * 10000 if u == "ha" and v else v
        typ = _type_from_title(name, "dom")
        city = addr.get("addressLocality") or None
        imgs = re.findall(r'(https://landelity\.com/img/\d+/\d+/\d+/[a-z]/i\.jpg)', html)
        imgs = list(dict.fromkeys(imgs)) or ([d["image"]] if d.get("image") else [])
        doc = {
            "source": "landelity", "external_id": ext, "url": d.get("url") or url.replace("/en/", "/pl/"),
            "title": re.sub(r"\s*\$[\d,.]+.*$", "", name).strip() + " — " + (f"{city}, {cname}" if city else cname), "title_orig": name,
            "type": typ, "transaction_type": "wynajem" if re.search(r"wynaj|rent", name.lower()) else "sprzedaz",
            "price": ppln, "price_pln": ppln, "price_eur": round(p) if p and cur == "EUR" else None, "price_usd": round(p) if p and cur == "USD" else None,
            "currency_orig": cur, "price_pm2": round(ppln / area_v) if ppln and area_v and area_v > 5 and typ != "dzialka" else None,
            "area_m2": area_v if typ != "dzialka" else None, "plot_area_m2": area_v if typ == "dzialka" else None, "rooms": None, "bedrooms": None, "bathrooms": None, "floor": None,
            "city": city, "region": addr.get("addressRegion") or None, "country": iso, "country_name": cname, "country_flag": flag,
            "location": ", ".join(x for x in [city, addr.get("addressRegion"), cname] if x),
            "image": imgs[0] if imgs else None, "images": imgs[:20], "description": (d.get("description") or "").replace("…", "").strip(),
            "seller_type": "prywatna", "agency_name": None,   # landelity = ogłoszenia bezpośrednio od właścicieli (free listing)
            "is_stale": False, "last_seen_at": datetime.now(timezone.utc).isoformat(), "posted_at": lastmod,
        }
        r = coll.update_one({"source": "landelity", "external_id": ext}, {"$set": doc, "$setOnInsert": {"added_at": datetime.now(timezone.utc).isoformat(), "scraped_via": "zagranica"}}, upsert=True)
        tot["new"] += int(r.upserted_id is not None); tot["updated"] += int(r.upserted_id is None and r.modified_count > 0)
    log.info("═══ DONE landelity: new=%d updated=%d pages=%d (inne kraje: %d) ═══", tot["new"], tot["updated"], tot["pages"], tot["skipped_country"])
    return tot


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
        gone = html is None or not any(k in html for k in ("data-price-PLN", "price-item", "info-price", "RealEstateListing"))
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
    run_greenacres(a.pages); run_landelity(150)
    if a.stale: mark_stale()
