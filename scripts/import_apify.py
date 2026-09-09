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

# UWAGA: kolejność ma znaczenie! Najbardziej specyficzne PIERWSZE.
# "Dom jednorodzinny w zabudowie szeregowej" → matchujemy szeregowca, NIE wolnostojącego.
DOM_TYPE_KEYWORDS = [
    # 1. SZEREGOWIEC - specyficzne wzorce
    ("szeregowiec",  [
        r"\bszeregow(?:iec|y|ego|ym|ej|a|e)\b",
        r"\bszeregowc(?:e|ów|ow|em|a)\b",
        r"szereg[oó]wk",                     # szeregówka
        r"dom(?:ek)?\s+w\s+szeregu",
        r"zabudow(?:a|ie|y)\s+szereg",
        r"\bw\s+szeregu\b",
        r"\bsegment(?:\s|,|\.|$|ow|em|u|y|owo)",
        r"townhouse", r"town\s?house",
        r"dom\s+szereg",
    ]),
    # 2. BLIZNIAK - specyficzne wzorce
    ("blizniak",     [
        r"bli[zźż]niak",
        r"bli[zźż]niacz",
        r"pół[- ]?bli[zźż]niak",
        r"pol[- ]?bli[zźż]niak",
        r"1[/\\]?2\s*bli[zźż]niak",
        r"po[łl]ow[aęą]\s+bli[zźż]niak",
        r"zabudow(?:a|ie|y)\s+bli[zźż]",
        r"\bdwurodzinn",
        r"\bduplex\b",
        r"dwupak",
    ]),
    # 3. SIEDLISKOWY
    ("siedliskowy",  [
        r"siedlisk",
        r"zabudowa\s+zagrodow",
        r"gospodarstw(?:o|em|a|u|ie)",
        r"\bstodo[łl]a\b",
        r"zagrod(?:a|y|owa)",
    ]),
    # 4. REZYDENCJA (przed letniskowy, bo willa/pałac)
    ("rezydencja",   [
        r"rezydencj",
        r"\bwilla\b", r"\bwill[ęeą]\b", r"willow",
        r"dwor(?:ek|ku|em|u|a|ów|ow)",
        r"\bpa[łl]ac",
        r"posiad[łl]o[śs][ćc]",
        r"apartament(?:owy|owa)\s+dom",
    ]),
    # 5. LETNISKOWY
    ("letniskowy",   [
        r"letnisk",
        r"dom(?:ek)?\s+rekreac",
        r"dom(?:ek)?\s+letni",
        r"\bROD\b",
        r"rekreacyjn(?:y|ego|ym|a|ej)\s+dom",
        r"domek\s+w\s+lesie",
        r"weekendow(?:y|ego|ym)\s+dom",
        r"wypoczynkow(?:y|ego|ym|a)\s+dom",
        r"dom\s+wypoczynkow",
        r"domek\s+mobiln",
    ]),
    # 6. WOLNOSTOJACY - najbardziej ogólny, OSTATNI (fallback)
    ("wolnostojacy", [
        r"wolno[- ]?stoj",
        r"wolnostoj",
        r"dom\s+wolno",
        r"jednorodzinn(?:y|ego|ym|a|ej|e)",
        r"parterow(?:y|ego|ym|a|ej|e)",
        r"pi[eę]trow(?:y|ego|ym|a|ej|e)",
        r"kanadyjs(?:ki|kie|kiego|ka|kim)",
        r"\bszkieletow(?:y|ego|ej|ym|a)",
        r"\bdom\s+z\s+bali\b",
        r"dom\s+pasywn",
        r"pasywn(?:y|ym|ego)\s+dom",
        r"dom\s+modu[łl]ow",
    ]),
]

# Mapowanie surowych wartości z portali (Otodom: dom_wolnostojacy, blizniak, szeregowiec, kamienica, rezydencja)
RAW_BUILDING_TYPE_MAP = {
    "dom_wolnostojacy": "wolnostojacy",
    "wolnostojacy": "wolnostojacy",
    "wolnostojący": "wolnostojacy",
    "detached": "wolnostojacy",
    "blizniak": "blizniak",
    "bliźniak": "blizniak",
    "semi_detached": "blizniak",
    "duplex": "blizniak",
    "szeregowiec": "szeregowiec",
    "szeregowy": "szeregowiec",
    "terraced": "szeregowiec",
    "townhouse": "szeregowiec",
    "segment": "szeregowiec",
    "kamienica": "rezydencja",
    "willa": "rezydencja",
    "rezydencja": "rezydencja",
    "letniskowy": "letniskowy",
    "domek_letniskowy": "letniskowy",
    "recreational": "letniskowy",
    "siedliskowy": "siedliskowy",
    "gospodarstwo": "siedliskowy",
}


def detect_from_raw(item: dict) -> str | None:
    """Sprawdź surowe pola z portali (Otodom, Morizon, Gratka)."""
    if not isinstance(item, dict): return None
    for key in ("buildingType", "building_type", "subtype", "homeType", "propertySubtype", "type_of_building"):
        v = str(item.get(key) or "").lower().strip()
        if not v: continue
        # bezpośredni match
        if v in RAW_BUILDING_TYPE_MAP:
            return RAW_BUILDING_TYPE_MAP[v]
        # fuzzy match
        for k, label in RAW_BUILDING_TYPE_MAP.items():
            if k in v or v in k:
                return label
    return None


def detect_from_url(url: str) -> str | None:
    """Otodom/Morizon URLs często zawierają typ w slugu."""
    if not url: return None
    u = url.lower()
    # Kolejność ma znaczenie
    if re.search(r"szeregow|szereg[oó]wk|-segment[- /]|/segment[- /]|townhouse", u): return "szeregowiec"
    if re.search(r"bli[zźż]niak|semi-?detached|duplex", u): return "blizniak"
    if re.search(r"siedlisk", u): return "siedliskowy"
    if re.search(r"rezydencj|-willa[- /]|/willa[- /]|dworek|pa[łl]ac", u): return "rezydencja"
    if re.search(r"letnisk|rekreacyjn", u): return "letniskowy"
    if re.search(r"wolnostoj|wolno-?stoj|jednorodzinn", u): return "wolnostojacy"
    return None


def detect_from_regex(text: str) -> str | None:
    if not text: return None
    t = text.lower()
    for label, kws in DOM_TYPE_KEYWORDS:
        for kw in kws:
            if re.search(kw, t):
                return label
    return None


def detect_dom_type(item_or_text, url: str = None) -> str | None:
    """
    Priorytet: raw field portalu → URL slug → regex title+description.
    Akceptuje zarówno dict (Apify item) jak i string (tylko tekst).
    """
    if isinstance(item_or_text, dict):
        r = detect_from_raw(item_or_text)
        if r: return r
        r = detect_from_url(item_or_text.get("url"))
        if r: return r
        text = f"{item_or_text.get('title','')} {item_or_text.get('descriptionText','') or item_or_text.get('description','')}"
        return detect_from_regex(text)
    # fallback dla starych wywołań (backfill)
    if url:
        r = detect_from_url(url)
        if r: return r
    return detect_from_regex(str(item_or_text or ""))

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

    # Dom subtype (wolnostojacy/blizniak/szeregowiec/etc) - z title+description+url+raw
    building_type = None
    if ptype == "dom":
        building_type = detect_dom_type(item)

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
        "building_type": building_type,
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
