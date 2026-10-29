#!/usr/bin/env python3
"""
Nieruchomosci-online.pl → MongoDB (listings) — scraper bezpośredni, bez proxy i bez kosztów zewnętrznych.

Strony listy i ofert są renderowane serwerowo i dostępne zwykłym HTTP. Lista jest domyślnie sortowana
„od najnowszych”, więc codzienny import pobiera tylko nowe ogłoszenia (przerywa, gdy strona nie wnosi nowych).
Dla nowych ofert pobierany jest szczegół: opis, telefon, zdjęcia, współrzędne, typ ogłoszeniodawcy.

Użycie:
  python scripts/scrape_nol.py --property mieszkanie [--transaction sprzedaz|wynajem] [--city krakow]
                               [--max-pages 6] [--max-details 1500]
Env: MONGODB_URI (lub MONGO_URL), MONGODB_DB (domyślnie finderdom)
"""
import argparse, html as htmllib, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_otodom import CITIES, DOM_SUBTYPES, DZIALKA_SUBTYPES, _num  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("nol")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,*/*"})

PROPERTY_SLUGS = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "dzialka"}
BASE_LABEL = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "działka"}


def list_url(prop: str, trans: str, city_name: str, page: int) -> str:
    u = f"https://www.nieruchomosci-online.pl/szukaj.html?3,{PROPERTY_SLUGS[prop]},{trans},,{requests.utils.quote(city_name)}"
    return u + (f"&p={page}" if page > 1 else "")


def fetch(url: str, tries: int = 3) -> Optional[str]:
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=25)
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            if r.status_code in (403, 429):
                log.warning("  HTTP %s — pauza 20s", r.status_code); time.sleep(20)
            elif r.status_code == 404:
                return None
        except requests.RequestException as e:
            log.warning("  fetch error: %s", str(e)[:100]); time.sleep(3)
    return None


def _txt(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _price(s: str) -> Optional[float]:
    m = re.search(r"([\d\s\xa0.]+)\s*zł", htmllib.unescape(s).replace("&nbsp;", " "))
    if not m: return None
    return _num(m.group(1).replace(" ", "").replace("\xa0", ""))


def parse_list(html: str) -> List[Dict[str, Any]]:
    out = []
    tiles = re.findall(r'<div class="tile tile-tile[^"]*"(.*?)(?=<div class="tile tile-tile|<div class="pagination-wrapper|$)', html, re.S)
    for t in tiles:
        m_id = re.search(r'data-id="a(\d+)"', t)
        m_url = re.search(r'href="(https://[a-z0-9-]+\.nieruchomosci-online\.pl/[^"]+/\d+\.html)"', t)
        if not m_id or not m_url: continue
        title_m = re.search(r'id="tertiary-name_\d+">(.*?)</a>', t, re.S)
        prov_m = re.search(r'id="tertiary-province_\d+">(.*?)</p>', t, re.S)
        price_m = re.search(r'id="primary-display_\d+">(.*?)</p>', t, re.S)
        img_m = re.search(r'<img src="(https://i\.st-nieruchomosci-online\.pl/[^"]+)"', t)
        market = re.search(r'data-market-type="([a-z]+)"', t)
        attrs = dict(re.findall(r'<p class="body-md"><span>([^<]*?):?</span>((?:<strong>[^<]*</strong>)+)', t))
        attrs = {k.strip().rstrip(":"): _txt(v.replace("</strong><strong>", "")) for k, v in attrs.items()}
        prov_txt = _txt(prov_m.group(1)) if prov_m else ""
        parts = [p.strip() for p in prov_txt.split(",") if p.strip()]
        area = None
        if price_m:
            am = re.search(r'class="area"[^>]*>(.*?)</span>', price_m.group(1), re.S)
            if am: area = _num(_txt(am.group(1)).replace("m²", "").replace(",", "."))
        out.append({
            "id": m_id.group(1), "url": m_url.group(1),
            "title": _txt(title_m.group(1)) if title_m else "",
            "district": parts[0] if len(parts) > 1 else None,
            "city_txt": parts[-1] if parts else None,
            "price": _price(price_m.group(1)) if price_m else None,
            "area": area,
            "image": img_m.group(1) if img_m else None,
            "market": {"secondary": "wtorny", "primary": "pierwotny"}.get(market.group(1) if market else "", None),
            "rooms": _num(attrs.get("Liczba pokoi")),
            "floor": attrs.get("Piętro"),
            "year": _num(attrs.get("Rok budowy")),
        })
    return out


def parse_detail(html: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    m = re.search(r"isPrivate:\s*'(\d)'", html)
    if m: d["is_private"] = m.group(1) == "1"
    m = re.search(r'offerType:\s*"([^"]*)"', html)
    if m: d["offer_type"] = m.group(1)
    m = re.search(r"isArchive:\s*(true|false)", html)
    if m: d["archived"] = m.group(1) == "true"
    m = re.search(r'photos:\s*(\{.*?\})\s*,\s*\n', html, re.S)
    if m:
        try:
            ph = json.loads(m.group(1))
            imgs = ph.get("x") or ph.get("l") or next((v for v in ph.values() if isinstance(v, list)), [])
            d["images"] = [u for u in imgs if isinstance(u, str) and u.startswith("http")][:20]
        except Exception:
            pass
    if not d.get("images"):
        d["images"] = list(dict.fromkeys(re.findall(r'https://i\.st-nieruchomosci-online\.pl/[^"\' ]+\.jpg', html)))[:20]
    m = re.search(r'href="tel:([+\d\s]+)"', html)
    if m: d["phone"] = re.sub(r"\s+", " ", m.group(1)).strip()
    la = re.search(r'"latitude"\s*:\s*"?(\d\d\.\d+)', html); lo = re.search(r'"longitude"\s*:\s*"?(\d\d\.\d+)', html)
    if la and lo: d["lat"], d["lng"] = float(la.group(1)), float(lo.group(1))
    # Opis: blok po nagłówku "Opis nieruchomości" do przycisku "Rozwiń opis"/kolejnej sekcji
    txt = _txt(re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S))
    i = txt.find("Opis nieruchomości")
    if i >= 0:
        seg = txt[i + len("Opis nieruchomości"):i + 6000]
        for stop in ("Rozwiń opis", "Zwiń opis", "Szczegóły oferty", "Lokalizacja"):
            j = seg.find(stop)
            if 50 < j: seg = seg[:j]
        d["description"] = seg.strip()[:4000]
    return d


def normalize(it: Dict[str, Any], det: Dict[str, Any], city_name: str, prop: str, trans: str) -> Dict[str, Any]:
    blob = f"{it['title']} {det.get('description', '')}".lower()
    subtype = None
    if prop == "dzialka":
        for label, kws in DZIALKA_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    elif prop == "dom":
        for label, kws in DOM_SUBTYPES:
            if any(k in blob for k in kws): subtype = label; break
    ptype = f"{BASE_LABEL[prop]} {subtype}" if subtype else BASE_LABEL[prop]
    seller = "posrednik"
    ot = (det.get("offer_type") or "").lower()
    if det.get("is_private") or "prywat" in ot: seller = "prywatna"
    elif "dewelop" in ot or "inwest" in ot: seller = "deweloper"
    price, area = it["price"], it["area"]
    city = it.get("city_txt") or city_name
    now_iso = datetime.now(timezone.utc).isoformat()
    doc: Dict[str, Any] = {
        "source": "nol", "external_id": it["id"], "type": ptype, "title": it["title"] or ptype.capitalize(),
        "url": it["url"], "city": city, "district": it.get("district"),
        "location": ", ".join(x for x in [city, it.get("district")] if x),
        "price": price, "area_m2": area,
        "price_pm2": round(price / area, 2) if price and area and area > 0 else None,
        "rooms": int(it["rooms"]) if it.get("rooms") else None,
        "floor": it.get("floor"), "year_built": int(it["year"]) if it.get("year") else None,
        "market_type": it.get("market"), "image": (det.get("images") or [it.get("image")] or [None])[0] or it.get("image"),
        "transaction_type": trans, "seller_type": seller, "last_seen_at": now_iso, "scraped_via": "nol-direct",
    }
    if subtype: doc["dzialka_type" if prop == "dzialka" else "building_type"] = subtype
    for k in ("images", "phone", "lat", "lng", "description"):
        if det.get(k) is not None: doc[k] = det[k]
    if det: doc["details_at"] = now_iso
    return doc


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url: log.error("MONGODB_URI not set"); sys.exit(1)
    c = MongoClient(url, serverSelectionTimeoutMS=8000); c.admin.command("ping")
    return c[os.environ.get("MONGODB_DB") or "finderdom"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", choices=list(PROPERTY_SLUGS), required=True)
    ap.add_argument("--transaction", choices=["sprzedaz", "wynajem"], default="sprzedaz")
    ap.add_argument("--city", help="slug miasta (np. krakow) — domyślnie 20 miast")
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--max-details", type=int, default=1500, help="ile szczegółów ofert pobrać w tym uruchomieniu")
    ap.add_argument("--delay", type=float, default=0.6)
    a = ap.parse_args()

    db = get_mongo(); coll = db["listings"]
    cities = CITIES if not a.city else [c for c in CITIES if c["slug"].split("/")[0] == a.city.lower()]
    if not cities: log.error("Unknown city"); sys.exit(1)
    log.info("═══ NOL scraper — %s / %s — cities=%d max_pages=%d ═══", a.transaction, a.property, len(cities), a.max_pages)
    tot = {"new": 0, "updated": 0, "details": 0, "pages": 0}

    for city in cities:
        log.info("── %s ──", city["name"])
        for page in range(1, a.max_pages + 1):
            html = fetch(list_url(a.property, a.transaction, city["name"], page))
            tot["pages"] += 1
            if not html: log.info("  page %d: brak HTML", page); break
            items = parse_list(html)
            if not items: log.info("  page %d: 0 ofert", page); break
            ids = [it["id"] for it in items]
            known = set(x["external_id"] for x in coll.find({"source": "nol", "external_id": {"$in": ids}}, {"external_id": 1}))
            ops = []
            for it in items:
                det: Dict[str, Any] = {}
                if it["id"] not in known and tot["details"] < a.max_details:
                    dh = fetch(it["url"]); tot["details"] += 1
                    if dh:
                        det = parse_detail(dh)
                        if det.get("archived"): continue
                    time.sleep(a.delay / 2)
                doc = normalize(it, det, city["name"], a.property, a.transaction)
                ops.append(UpdateOne({"source": "nol", "external_id": it["id"]},
                                     {"$set": doc, "$setOnInsert": {"added_at": doc["last_seen_at"], "posted_at": None},
                                      "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}},
                                     upsert=True))
            new = upd = 0
            if ops:
                try:
                    r = coll.bulk_write(ops, ordered=False); new, upd = r.upserted_count, r.modified_count
                except Exception as e:
                    log.warning("  bulk_write: %s", str(e)[:200])
            tot["new"] += new; tot["updated"] += upd
            log.info("  page %d: %d ofert → new=%d upd=%d (details=%d)", page, len(items), new, upd, tot["details"])
            if new == 0 and page >= 2:
                log.info("  → brak nowych, następne miasto"); break
            time.sleep(a.delay)

    log.info("═══ DONE %s/%s: new=%d updated=%d details=%d pages=%d ═══",
             a.transaction, a.property, tot["new"], tot["updated"], tot["details"], tot["pages"])


if __name__ == "__main__":
    main()
