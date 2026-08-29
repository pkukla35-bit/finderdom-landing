#!/usr/bin/env python3
"""
FinderDom.pl — Otodom scraper via Apify (ahmed_jasarevic actor).

Fetches property listings from Otodom via Apify's managed scraper,
converts them into FinderDom's `listings.json` format, and MERGES them
into the existing base (never destroys prior data).

Cost: ~$0.002 per listing (Apify actor ahmed_jasarevic/otodom-pl-property-scraper).

Usage:
  APIFY_TOKEN=apify_api_xxx python scripts/apify_otodom_scraper.py

Env vars:
  APIFY_TOKEN                — required
  LISTINGS_JSON_PATH         — default: data/listings.json
  MIN_LISTINGS_THRESHOLD     — default: 500 (fail-safe; refuses to overwrite base if fewer)
  MAX_PER_CITY               — default: 100
  DRY_RUN                    — set to "1" to skip saving
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


APIFY_TOKEN = os.environ.get("APIFY_TOKEN") or ""
if not APIFY_TOKEN:
    print("ERROR: APIFY_TOKEN env var missing", file=sys.stderr)
    sys.exit(2)

ACTOR = "ahmed_jasarevic~otodom-pl-property-scraper"
LISTINGS_PATH = Path(os.environ.get("LISTINGS_JSON_PATH", "data/listings.json"))
MIN_LISTINGS_THRESHOLD = int(os.environ.get("MIN_LISTINGS_THRESHOLD", "500"))
MAX_PER_CITY = int(os.environ.get("MAX_PER_CITY", "100"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# Cities to scrape (Otodom URL slugs). Order = priority.
CITIES = [
    ("mazowieckie/warszawa/warszawa/warszawa", "Warszawa"),
    ("malopolskie/krakow/krakow/krakow", "Kraków"),
    ("dolnoslaskie/wroclaw/wroclaw/wroclaw", "Wrocław"),
    ("wielkopolskie/poznan/poznan/poznan", "Poznań"),
    ("pomorskie/gdansk/gdansk/gdansk", "Gdańsk"),
    ("lodzkie/lodz/lodz/lodz", "Łódź"),
    ("slaskie/katowice/katowice/katowice", "Katowice"),
    ("lubelskie/lublin/lublin/lublin", "Lublin"),
    ("kujawsko--pomorskie/bydgoszcz/bydgoszcz/bydgoszcz", "Bydgoszcz"),
    ("zachodniopomorskie/szczecin/szczecin/szczecin", "Szczecin"),
    # Małopolska (dla wycen Głogoczów, Wieliczka itd)
    ("malopolskie/wielicki/wieliczka/wieliczka", "Wieliczka"),
    ("malopolskie/krakowski/skawina/skawina", "Skawina"),
    ("malopolskie/myslenicki/myslenice/myslenice", "Myślenice"),
    ("malopolskie/bochenski/bochnia/bochnia", "Bochnia"),
    ("malopolskie/krakowski/niepolomice/niepolomice", "Niepołomice"),
    ("malopolskie/krakowski/zielonki/zielonki", "Zielonki"),
]

# (property_type, transaction) combinations
COMBOS = [
    ("mieszkanie", "sprzedaz"),
    ("dom", "sprzedaz"),
    ("dzialka", "sprzedaz"),
    ("mieszkanie", "wynajem"),
]


def _fmt_price(v):
    try:
        return f"{int(v):,}".replace(",", " ") + " zł"
    except Exception:
        return "—"


def _fmt_pm2(v):
    try:
        return f"{int(v):,}".replace(",", " ") + " zł/m²"
    except Exception:
        return "—"


def _added_display(iso_dt):
    if not iso_dt:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        days = diff.days
        if days == 0:
            hours = diff.seconds // 3600
            return f"{hours}h temu" if hours > 0 else "przed chwilą"
        if days == 1:
            return "wczoraj"
        if days < 7:
            return f"{days} dni temu"
        if days < 30:
            return f"{days // 7} tyg. temu"
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return "—"


def _map_property_type(pt):
    """Otodom → FinderDom type."""
    if not pt:
        return "mieszkanie"
    pt = pt.lower()
    if "mieszk" in pt:
        return "mieszkanie"
    if "dom" in pt:
        return "dom"
    if "dzial" in pt or "działka" in pt:
        return "dzialka"
    if "lokal" in pt:
        return "lokal"
    if "hale" in pt or "magazyn" in pt:
        return "magazyn"
    if "garaz" in pt or "garaż" in pt:
        return "garaz"
    return pt


def convert(apify_item):
    """Convert Apify ahmed_jasarevic output → FinderDom listings.json format."""
    listing_id = apify_item.get("listingId") or apify_item.get("id")
    if not listing_id:
        return None

    price = apify_item.get("price")
    if isinstance(price, dict):
        price = price.get("value")
    price_pm2 = apify_item.get("pricePerSqm")
    if isinstance(price_pm2, dict):
        price_pm2 = price_pm2.get("value")

    area = apify_item.get("areaSqm")
    photos = apify_item.get("photos") or []

    ptype = _map_property_type(apify_item.get("propertyType"))
    txn = "sprzedaz" if (apify_item.get("transaction") or "").lower() in ("sell", "sale", "sprzedaz", "sprzedaż") else "wynajem"

    market_raw = (apify_item.get("market") or "").lower()
    market_type = "pierwotny" if "primary" in market_raw else "wtorny"
    standard = "dobry"  # sensible default

    advertiser = (apify_item.get("advertiserType") or "").lower()
    if apify_item.get("agencyName") or (advertiser in ("agency", "business")):
        seller_type = "agency"
        seller_label = "Pośrednik"
    else:
        seller_type = "private"
        seller_label = "Prywatna"

    features = apify_item.get("features") or []
    has_parking = any(("parking" in str(f).lower() or "garaz" in str(f).lower() or "garaż" in str(f).lower()) for f in features)

    return {
        "id": f"otodom-{listing_id}",
        "type": ptype,
        "transaction": txn,
        "title": apify_item.get("title") or "",
        "city": apify_item.get("city") or "",
        "district": apify_item.get("district") or "",
        "sub_location": apify_item.get("subdistrict") or apify_item.get("street") or "",
        "location": apify_item.get("province") or "",
        "area_m2": area,
        "rooms": apify_item.get("rooms"),
        "floor": apify_item.get("floor"),
        "max_floor": apify_item.get("buildingFloors"),
        "year_built": apify_item.get("buildYear"),
        "standard": standard,
        "market_type": market_type,
        "price": int(price) if price else None,
        "price_pm2": int(price_pm2) if price_pm2 else None,
        "price_display": _fmt_price(price),
        "price_pm2_display": _fmt_pm2(price_pm2),
        "portal": "otodom",
        "seller_type": seller_type,
        "seller_label": seller_label,
        "source_url": apify_item.get("url") or "",
        "image_url": photos[0] if photos else "",
        "emoji": {"mieszkanie": "🏢", "dom": "🏠", "dzialka": "🌳", "lokal": "🏪", "magazyn": "🏭", "garaz": "🚗"}.get(ptype, "🏠"),
        "img_class": "photo",
        "added_at": apify_item.get("publishDate") or apify_item.get("modifiedAt") or "",
        "added_display": _added_display(apify_item.get("publishDate")),
        "verdict_badge": "W NORMIE",
        "verdict_text": "",
        "verdict_full": "",
        "ai_delta_pct": 0,
        "ai_offers_pm2": price_pm2,
        "ai_rcn_pm2": int(price_pm2 * 0.94) if price_pm2 else None,
        "is_original": True,
        "duplicate_of": None,
        "has_parking": has_parking,
        "lat": apify_item.get("latitude"),
        "lon": apify_item.get("longitude"),
        "agent_name": apify_item.get("agentName") or "",
        "agent_phones": apify_item.get("agentPhones") or [],
        "agency_name": apify_item.get("agencyName") or "",
    }


def scrape_one(location_slug, property_type, transaction, limit):
    """Call Apify actor for a single location+type+txn combo. Blocking, up to 5min."""
    url = f"https://www.otodom.pl/pl/wyniki/{transaction}/{property_type}/{location_slug}"
    api_url = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    payload = {
        "startUrls": [{"url": url}],
        "maxResults": limit,
    }
    try:
        with httpx.Client(timeout=300) as c:
            r = c.post(api_url, json=payload)
        if r.status_code != 201 and r.status_code != 200:
            print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return []
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            print(f"  ✗ {data['error'].get('type')}: {data['error'].get('message','')[:120]}", file=sys.stderr)
            return []
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ✗ Exception: {str(e)[:200]}", file=sys.stderr)
        return []


def main():
    print(f"→ FinderDom Apify scraper starting")
    print(f"  Actor: {ACTOR}")
    print(f"  Cities: {len(CITIES)}, Combos: {len(COMBOS)}, Max/combo: {MAX_PER_CITY}")
    print(f"  Est. max cost: ${len(CITIES) * len(COMBOS) * MAX_PER_CITY * 0.0019:.2f}")
    print()

    all_new = {}  # id -> listing (dedupe by id)
    total_calls = 0
    total_ok = 0

    for slug, city_name in CITIES:
        for ptype, txn in COMBOS:
            total_calls += 1
            print(f"[{total_calls}/{len(CITIES)*len(COMBOS)}] {city_name} {ptype}/{txn} …", end=" ", flush=True)
            items = scrape_one(slug, ptype, txn, MAX_PER_CITY)
            print(f"{len(items)} results")
            total_ok += len(items)
            for it in items:
                conv = convert(it)
                if conv:
                    all_new[conv["id"]] = conv
            time.sleep(0.5)  # be polite to Apify

    print()
    print(f"→ Total unique new listings: {len(all_new)} (from {total_ok} raw items, {total_calls} calls)")

    if len(all_new) < MIN_LISTINGS_THRESHOLD:
        print(f"✗ FAIL-SAFE: got only {len(all_new)} listings (< {MIN_LISTINGS_THRESHOLD}). Refusing to save. Base preserved.")
        sys.exit(1)

    if DRY_RUN:
        print("DRY_RUN=1 — not saving to disk.")
        return

    # Merge with existing base
    if LISTINGS_PATH.exists():
        with open(LISTINGS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_list = existing if isinstance(existing, list) else existing.get("listings", [])
        existing_by_id = {x.get("id"): x for x in existing_list if x.get("id")}
        print(f"→ Existing base: {len(existing_by_id)} listings")
    else:
        existing_by_id = {}
        print("→ No existing base — creating fresh")

    # Merge: new overrides existing (Apify data is fresher)
    merged = dict(existing_by_id)
    merged.update(all_new)
    merged_list = list(merged.values())

    LISTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LISTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged_list, f, ensure_ascii=False)
    print(f"✓ Saved {len(merged_list)} listings to {LISTINGS_PATH} ({len(all_new)} new/updated)")


if __name__ == "__main__":
    main()
