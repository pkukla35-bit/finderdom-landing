#!/usr/bin/env python3
"""
Import ofert z Apify dataset do MongoDB w schemacie kompatybilnym z frontendem finderdom.pl

Użycie:
  python scripts/import_apify.py --dataset-id XXX [--source apify_aggregator]

Konwertuje pola aggregatora Apify (trev0n/polish-real-estate-aggregator) do naszego schematu:
  external_id, type, title, url, image, images[], location, city, district,
  price, area_m2, price_pm2, rooms, lat, lng, transaction_type, added_at,
  posted_at, seller_type, market_type, description, agency, phone,
  dzialka_type (auto-wykryty z opisu), sources[] (dla deduplikacji cross-portal)
"""
import argparse, logging, os, re, sys, urllib.request, json, ssl
from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("import_apify")

DZIALKA_KEYWORDS = [
    ("budowlana",    [r"budowlan", r"\bzabudowa\s+jednorodzin", r"\bmn\b"]),
    ("rolna",        [r"\brolna\b", r"\brolne\b", r"\brolny\b", r"orn[ae]", r"uprawn", r"\bR\d\b"]),
    ("rekreacyjna",  [r"rekreacyj", r"letnisk", r"weekendow", r"\brod\b", r"ogrodow"]),
    ("lesna",        [r"leśn", r"lesn", r"\blasu\b", r"w lesie", r"zalesion", r"\bZL\b"]),
    ("inwestycyjna", [r"inwestycyj", r"komercyj", r"usługow", r"uslugow", r"przemysłow", r"przemyslow"]),
]

def detect_purpose(text: str) -> str | None:
    if not text: return None
    t = text.lower()
    for label, kws in DZIALKA_KEYWORDS:
        for kw in kws:
            if re.search(kw, t):
                return label
    return None


def to_iso(d) -> str | None:
    if not d: return None
    try:
        if isinstance(d, str):
            s = d.replace("Z", "+00:00")
            return datetime.fromisoformat(s).isoformat()
    except: pass
    return None


def normalize(item: dict, prop_type_hint: str = None) -> dict | None:
    """Konwertuj Apify aggregator item → schemat naszej bazy MongoDB."""
    offer_id = item.get("offerId") or item.get("clusterId")
    if not offer_id: return None
    url = item.get("url")
    if not url: return None

    # Type detection — z opisu / tytułu jeśli hint niepodany
    ptype = (prop_type_hint or "").lower()
    if not ptype:
        text = f"{item.get('title','')} {item.get('descriptionText','')}".lower()
        if "działka" in text or "dzialka" in text: ptype = "dzialka"
        elif "dom" in text and "mieszk" not in text: ptype = "dom"
        else: ptype = "mieszkanie"

    # Land purpose - detekcja z pełnego opisu
    dzialka_type = None
    if ptype == "dzialka":
        combined = f"{item.get('title','')} {item.get('descriptionText','')}"
        dzialka_type = detect_purpose(combined)

    # Seller type mapping
    st = str(item.get("sellerType") or item.get("sellerCategory") or "").lower()
    if st in ("business", "agency", "posrednik"): seller_type = "posrednik"
    elif st in ("private", "prywatna", "owner"): seller_type = "prywatna"
    elif st == "developer": seller_type = "deweloper"
    else: seller_type = "posrednik"

    posted = to_iso(item.get("dateCreated") or item.get("datePublished"))
    now_iso = datetime.now(timezone.utc).isoformat()

    # Location
    city = item.get("city") or ""
    if city == "krakow": city = "Kraków"
    elif city == "warszawa": city = "Warszawa"
    elif city == "wroclaw": city = "Wrocław"
    elif city == "poznan": city = "Poznań"
    elif city == "gdansk": city = "Gdańsk"
    elif city == "lodz": city = "Łódź"
    elif city == "krakow": city = "Kraków"
    else: city = city.capitalize()
    district = item.get("district") or ""
    location = ", ".join(x for x in [city, district] if x)

    # Price / area
    price = int(item.get("price") or 0) if item.get("price") else None
    area = float(item.get("area") or 0) if item.get("area") else None
    ppm2 = round(price / area, 2) if price and area else None

    images = item.get("imageUrls") or []
    img = images[0] if images else None

    return {
        "source": "apify",
        "source_actor": "trev0n/polish-real-estate-aggregator",
        "external_id": str(offer_id),
        "cluster_id": item.get("clusterId"),
        "type": ptype,
        "title": item.get("title") or "",
        "url": url,
        "location": location,
        "city": city,
        "district": district,
        "price": price,
        "area_m2": area,
        "price_pm2": ppm2,
        "rooms": item.get("rooms"),
        "floor": item.get("floor"),
        "build_year": item.get("buildYear"),
        "lat": item.get("latitude"),
        "lng": item.get("longitude"),
        "image": img,
        "images": images,
        "transaction_type": "sprzedaz",
        "seller_type": seller_type,
        "market_type": item.get("marketType"),
        "agency": item.get("agency"),
        "phone": item.get("phone"),
        "description": (item.get("descriptionText") or "")[:2000],
        "sources": item.get("sources") or [{"portal": item.get("portal"), "url": url}],
        "portals": item.get("portals") or [item.get("portal")],
        "added_at": posted or now_iso,
        "posted_at": posted,
        "last_seen_at": now_iso,
        "dzialka_type": dzialka_type,
        "scraped_via": "apify",
    }


def fetch_dataset(dataset_id: str, token: str) -> list:
    """Iteracyjne pobieranie datasetu (paginacja co 1000)."""
    all_items = []
    offset = 0
    limit = 1000
    ctx = ssl.create_default_context()
    while True:
        url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}&format=json&offset={offset}&limit={limit}"
        with urllib.request.urlopen(url, context=ctx, timeout=30) as r:
            items = json.loads(r.read())
        if not items: break
        all_items.extend(items)
        log.info(f"  fetched {len(all_items)} items so far")
        if len(items) < limit: break
        offset += limit
    return all_items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--property", type=str, default="", help="Hint typu (dzialka/dom/mieszkanie)")
    args = ap.parse_args()

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        log.error("Brak APIFY_TOKEN w env"); sys.exit(1)
    mongo_uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not mongo_uri:
        log.error("Brak MONGODB_URI"); sys.exit(1)
    db_name = os.environ.get("MONGODB_DB", "finderdom")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[db_name]
    coll = db.listings

    log.info(f"Pobieram dataset {args.dataset_id}…")
    items = fetch_dataset(args.dataset_id, token)
    log.info(f"Otrzymano {len(items)} items z Apify")

    docs = [normalize(it, args.property) for it in items]
    docs = [d for d in docs if d]
    log.info(f"Znormalizowano {len(docs)} ofert")

    if not docs: return

    ops = [UpdateOne({"source": d["source"], "external_id": d["external_id"]},
                     {"$set": d}, upsert=True) for d in docs]
    r = coll.bulk_write(ops, ordered=False)
    log.info(f"KONIEC: upserted={r.upserted_count} modified={r.modified_count}")


if __name__ == "__main__":
    main()
