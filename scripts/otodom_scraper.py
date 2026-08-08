"""FinderDom — Otodom scraper produkcyjny (v2).

Strategia:
  - Pobiera najnowsze ogłoszenia z całej Polski (`cala-polska`) sortowane po LATEST.
  - 9 kombinacji: (sprzedaz|wynajem) × (mieszkanie|dom|dzialka|lokal|pokoj|garaz).
  - Każde miasto Polski jest reprezentowane automatycznie (bez mapy miast).
  - Konfigurowalne przez zmienne środowiskowe (na potrzeby GitHub Actions).

Anti-bot:
  - Rotacja User-Agent, opóźnienia 1.5-3s, retry z exp backoff.
  - Pobiera JSON z `<script id="__NEXT_DATA__">` – szybko i stabilnie.
  - ScraperAPI (residential proxy pool) — automatyczny bypass anti-bot.

Safe-fail:
  - Jeśli scrape < MIN_LISTINGS (100) → NIE nadpisuje listings.json.
  - Zawsze tworzy backup poprzedniego pliku (listings.backup.json).
  - Wypisuje jasny błąd i wychodzi z kodem != 0 (widoczne w GH Actions).

Uruchomienie lokalne:
  python3 otodom_scraper.py
  MAX_PAGES=3 python3 otodom_scraper.py         # szybki test
  MAX_LISTINGS=1000 python3 otodom_scraper.py   # limit output
"""
from __future__ import annotations

import json
import os
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("[FATAL] requests not installed. pip install requests")
    sys.exit(2)


# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
MAX_PAGES = int(os.getenv("MAX_PAGES", "8"))
MAX_LISTINGS = int(os.getenv("MAX_LISTINGS", "60000"))
MIN_LISTINGS = int(os.getenv("MIN_LISTINGS", "100"))
DELAY_MIN = float(os.getenv("DELAY_MIN", "1.2"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "2.5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))

# ---- ScraperAPI (residential proxy pool + anti-bot bypass) ----
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
SCRAPER_API_ENABLED = bool(SCRAPER_API_KEY)
SCRAPER_API_PREMIUM = os.getenv("SCRAPER_API_PREMIUM", "1") == "1"
SCRAPER_API_COUNTRY = os.getenv("SCRAPER_API_COUNTRY", "pl")
SCRAPER_API_ENDPOINT = "http://api.scraperapi.com/"

# Detail fetching (Faza 2)
FETCH_DETAILS = os.getenv("FETCH_DETAILS", "1") == "1"
SKIP_DETAILS = os.getenv("SKIP_DETAILS", "0") == "1"
DETAILS_ONLY = os.getenv("DETAILS_ONLY", "0") == "1"
DETAIL_WORKERS = int(os.getenv("DETAIL_WORKERS", "6"))
DETAIL_TIMEOUT = int(os.getenv("DETAIL_TIMEOUT", "15"))
DETAIL_ONLY_ORIGINALS = os.getenv("DETAIL_ONLY_ORIGINALS", "1") == "1"
DETAIL_MAX = int(os.getenv("DETAIL_MAX", "60000"))
DETAIL_DELAY = float(os.getenv("DETAIL_DELAY", "0.3"))
DETAIL_MAX_RETRIES = int(os.getenv("DETAIL_MAX_RETRIES", "2"))

# Per-voivodeship scraping
SCRAPE_VOIVODESHIPS = os.getenv("SCRAPE_VOIVODESHIPS", "1") == "1"
VOJ_MAX_PAGES = int(os.getenv("VOJ_MAX_PAGES", "20"))

VOIVODESHIPS = [
    "dolnoslaskie",
    "kujawsko--pomorskie",
    "lubelskie",
    "lubuskie",
    "lodzkie",
    "malopolskie",
    "mazowieckie",
    "opolskie",
    "podkarpackie",
    "podlaskie",
    "pomorskie",
    "slaskie",
    "swietokrzyskie",
    "warminsko--mazurskie",
    "wielkopolskie",
    "zachodniopomorskie",
]

VOJ_COMBOS = [
    ("sprzedaz", "mieszkanie", "mieszkanie", "pierwotny", "PRIMARY"),
    ("sprzedaz", "mieszkanie", "mieszkanie", "wtorny",    "SECONDARY"),
    ("sprzedaz", "dom",        "dom",        "pierwotny", "PRIMARY"),
    ("sprzedaz", "dom",        "dom",        "wtorny",    "SECONDARY"),
    ("sprzedaz", "dzialka",    "dzialka",    "",          ""),
    ("sprzedaz", "lokal",      "lokal",      "pierwotny", "PRIMARY"),
    ("sprzedaz", "lokal",      "lokal",      "wtorny",    "SECONDARY"),
    ("wynajem",  "mieszkanie", "mieszkanie", "",          ""),
    ("wynajem",  "dom",        "dom",        "",          ""),
]

OUT_DIR = Path(os.getenv(
    "OUT_DIR",
    str(Path(__file__).resolve().parent.parent / "data"),
))
OUT_FILE = OUT_DIR / "listings.json"
BACKUP_FILE = OUT_DIR / "listings.backup.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

COMBOS = [
    ("sprzedaz", "mieszkanie", "mieszkanie"),
    ("sprzedaz", "dom", "dom"),
    ("sprzedaz", "dzialka", "dzialka"),
    ("sprzedaz", "lokal", "lokal"),
    ("sprzedaz", "garaz", "garaz"),
    ("wynajem", "mieszkanie", "mieszkanie"),
    ("wynajem", "dom", "dom"),
    ("wynajem", "pokoj", "pokoj"),
    ("wynajem", "lokal", "lokal"),
]

ROOMS_ENUM = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4,
    "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9,
    "MORE": 10, "TEN_AND_MORE": 10,
}

TYPE_EMOJI = {
    "mieszkanie": ["🏢", "🏘️", "🏛️", "🌆", "🏙️"],
    "dom": ["🏠", "🏡", "🏘️"],
    "dzialka": ["🌳", "🌾", "🌲", "🏞️"],
    "lokal": ["🏬", "🏪", "🏢"],
    "pokoj": ["🚪", "🛏️"],
    "garaz": ["🚗", "🏚️"],
}
IMG_CLASSES = ["i1", "i2", "i3", "i4", "i5", "i6"]

NEXT_MARKER = '<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def scraper_get(url: str, timeout: int = REQUEST_TIMEOUT):
    """Uniwersalny wrapper HTTP:
       - Jeśli ustawione SCRAPER_API_KEY → routing przez ScraperAPI (proxy pool + anti-bot).
       - Inaczej → bezpośredni requests.get (dev/local).
    """
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Referer": "https://www.otodom.pl/",
    }
    try:
        if SCRAPER_API_ENABLED:
            params = {
                "api_key": SCRAPER_API_KEY,
                "url": url,
                "country_code": SCRAPER_API_COUNTRY,
                "keep_headers": "true",
            }
            if SCRAPER_API_PREMIUM:
                params["premium"] = "true"
            return requests.get(
                SCRAPER_API_ENDPOINT,
                params=params,
                headers=headers,
                timeout=max(timeout, 70),
            )
        else:
            return requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException:
        return None


def fetch_page(url: str):
    """Pobierz stronę Otodom + wyciągnij __NEXT_DATA__. Retry z backoffem."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        r = scraper_get(url, timeout=REQUEST_TIMEOUT)
        if r is None:
            last_err = "network error"
        elif r.status_code == 200:
            start = r.text.find(NEXT_MARKER)
            if start == -1:
                last_err = "__NEXT_DATA__ not found (possibly bot page)"
            else:
                start += len(NEXT_MARKER)
                end = r.text.find("</script>", start)
                if end == -1:
                    last_err = "closing </script> not found"
                else:
                    try:
                        return json.loads(r.text[start:end])
                    except json.JSONDecodeError as e:
                        last_err = f"JSON decode: {e}"
        elif r.status_code in (403, 429, 503, 500, 502, 504):
            last_err = f"HTTP {r.status_code}"
            time.sleep(min(4 * (2 ** (attempt - 1)), 32) + random.uniform(0, 2))
        else:
            last_err = f"HTTP {r.status_code}"
        if attempt < MAX_RETRIES:
            time.sleep(2 * attempt + random.uniform(0, 1))
    print(f"    [retry x{MAX_RETRIES} FAILED] {last_err}")
    return None


# --------------------------------------------------------------------------
# TRANSFORM
# --------------------------------------------------------------------------
def normalize_city(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    trans = str.maketrans("ąćęłńóśźż", "acelnoszz")
    return re.sub(r"\s+", "-", s.translate(trans))


def extract_city_district(item: dict):
    city, district = "", ""
    try:
        locs = item.get("location", {}).get("reverseGeocoding", {}).get("locations", [])
        for lo in locs:
            lvl = lo.get("locationLevel", "")
            if lvl == "city_or_village" and not city:
                city = lo.get("name", "")
            elif lvl == "district" and not district:
                district = lo.get("name", "")
        if not city:
            city = item.get("location", {}).get("address", {}).get("city", {}).get("name", "")
    except (AttributeError, TypeError, KeyError):
        pass
    return city, district


def detect_seller(item: dict):
    if item.get("isPrivateOwner"):
        return "prywatna", "👨‍👩‍👧 Prywatna"
    if item.get("isDeveloperOwner"):
        return "deweloper", "🏗️ Deweloper"
    adv_type = (item.get("extendedAdvertiserType") or "").upper()
    if adv_type == "PRIVATE":
        return "prywatna", "👨‍👩‍👧 Prywatna"
    if adv_type == "DEVELOPER":
        return "deweloper", "🏗️ Deweloper"
    if item.get("agency") or adv_type in ("AGENT", "AGENCY", "BUSINESS"):
        return "posrednik", "🏢 Pośrednik"
    return "posrednik", "🏢 Pośrednik"


def transform(item: dict, transaction: str, typ_out: str):
    try:
        tp = item.get("totalPrice") or {}
        rp = item.get("rentPrice") or {}
        price = tp.get("value") if tp else None
        if not price:
            price = rp.get("value") if rp else None
        if not price or price < 1000:
            return None

        area = item.get("areaInSquareMeters") or item.get("terrainAreaInSquareMeters") or 0
        if not area or area < 1:
            return None

        pm2_field = item.get("pricePerSquareMeter") or {}
        ppm2 = int(pm2_field.get("value") or 0) if pm2_field else 0
        if not ppm2 and area > 0:
            ppm2 = int(price / area)

        rooms_raw = item.get("roomsNumber")
        rooms = ROOMS_ENUM.get(rooms_raw, 0) if isinstance(rooms_raw, str) else (
            int(rooms_raw) if isinstance(rooms_raw, (int, float)) else 0)

        floor_raw = item.get("floorNumber") or ""
        floor_num = 0
        if isinstance(floor_raw, str):
            m = re.search(r"(\d+)", floor_raw)
            if m:
                floor_num = int(m.group(1))
            elif "GROUND" in floor_raw.upper() or "PARTER" in floor_raw.upper():
                floor_num = 0
        elif isinstance(floor_raw, (int, float)):
            floor_num = int(floor_raw)

        city, district = extract_city_district(item)
        if not city:
            return None

        seller_type, seller_label = detect_seller(item)

        date_str = item.get("dateCreated") or item.get("createdAtFirst") or ""
        added_iso = date_str
        added_display = ""
        try:
            dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            added_iso = dt.isoformat()
            diff = datetime.now(timezone.utc) - dt
            hrs = int(diff.total_seconds() // 3600)
            if hrs < 1:
                added_display = "przed chwilą"
            elif hrs < 48:
                added_display = f"{hrs}h temu"
            else:
                added_display = f"{hrs // 24}d temu"
        except (ValueError, TypeError):
            added_display = ""

        slug = item.get("slug", "")
        source_url = f"https://www.otodom.pl/pl/oferta/{slug}" if slug else ""

        title = (item.get("title") or "")[:120]

        images = item.get("images") or []
        img_url = ""
        if images and isinstance(images[0], dict):
            img_url = images[0].get("large") or images[0].get("medium") or ""

        emoji = random.choice(TYPE_EMOJI.get(typ_out, ["🏢"]))

        return {
            "id": f"otodom-{item.get('id')}",
            "type": typ_out,
            "transaction": transaction,
            "title": title,
            "city": city,
            "district": district,
            "location": f"📍 {city}{', ' + district if district else ''}",
            "area_m2": round(float(area), 1),
            "rooms": rooms,
            "floor": floor_num,
            "max_floor": 0,
            "year_built": 0,
            "standard": "",
            "price": int(price),
            "price_pm2": ppm2,
            "price_display": f"{int(price):,} zł".replace(",", " "),
            "price_pm2_display": f"{ppm2:,} zł/m²".replace(",", " "),
            "portal": "Otodom",
            "seller_type": seller_type,
            "seller_label": seller_label,
            "source_url": source_url,
            "image_url": img_url,
            "emoji": emoji,
            "img_class": random.choice(IMG_CLASSES),
            "added_at": added_iso,
            "added_display": added_display,
            "verdict_badge": "normal",
            "verdict_text": "✓ W NORMIE",
            "verdict_full": "CENA ZGODNA Z RYNKIEM",
            "ai_delta_pct": 0,
            "ai_offers_pm2": 0,
            "ai_rcn_pm2": 0,
            "is_original": True,
            "duplicate_of": None,
            "_fingerprint": "",
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# TITLE PARSING
# --------------------------------------------------------------------------
BUILDING_TYPE_KEYWORDS = [
    (re.compile(r"\b(bli[żz]niak|pol[oó]w[ka]?\s+dom)", re.I), "blizniak"),
    (re.compile(r"\b(szeregow|segment|w\s+szereg|zabudowa\s+szer|zabudowie\s+szer)", re.I), "szeregowiec"),
    (re.compile(r"\b(siedlisk|gospodarstw|zagrod|d[wr]orek)", re.I), "siedliskowy"),
    (re.compile(r"\b(letnisk|weekend|ca[łl]oroczn|domek\s+let|dzia[łl]k[oa]wy)", re.I), "letniskowy"),
    (re.compile(r"\b(rezydencj|pa[łl]ac|posiad[łl]o|willa|villa)", re.I), "rezydencja"),
    (re.compile(r"\bwolnostoj|wolno.?stoj|osobno\s+stoj", re.I), "wolnostojacy"),
    (re.compile(r"\bapartament(?!ow)", re.I), "apartamentowiec"),
    (re.compile(r"\bkamienic", re.I), "kamienica"),
    (re.compile(r"\bloft\b", re.I), "loft"),
    (re.compile(r"\bblok\b|w\s+bloku|z\s+wielkiej\s+p[łl]yty", re.I), "blok"),
]

STANDARD_KEYWORDS = [
    (re.compile(r"\bdo\s+remontu|generalnego\s+remontu|wymaga\s+remontu", re.I), "do_remontu"),
    (re.compile(r"\bdo\s+wyko[nń]czenia|deweloperski|stan\s+deweloper", re.I), "do_wykonczenia"),
    (re.compile(r"\bgotow[ea]?\s+do\s+zam|do\s+zamieszkania|wyko[nń]czone|po\s+remoncie|urz[aą]dzone", re.I), "gotowe"),
]

BOOL_KEYWORDS = {
    "has_balcony": re.compile(r"\bbalkon", re.I),
    "has_terrace": re.compile(r"\btaras", re.I),
    "has_garden": re.compile(r"\bogr[oó]d|ogr[oó]dek", re.I),
    "has_lift": re.compile(r"\bwinda|wind[ąa]|z\s+wind", re.I),
    "has_garage": re.compile(r"\bgara[żz]|z\s+gara|miejsce\s+w\s+gara", re.I),
    "has_basement": re.compile(r"\bpiwnic", re.I),
    "has_parking": re.compile(r"\bparking|miejsce\s+posto|miejsce\s+park", re.I),
}


def parse_title_hints(listing: dict) -> None:
    text = f"{listing.get('title','')} {listing.get('description','')}"
    if not text.strip():
        return
    if not listing.get("building_type"):
        for pat, val in BUILDING_TYPE_KEYWORDS:
            if pat.search(text):
                listing["building_type"] = val
                break
    if not listing.get("standard"):
        for pat, val in STANDARD_KEYWORDS:
            if pat.search(text):
                listing["standard"] = val
                break
    for key, pat in BOOL_KEYWORDS.items():
        if not listing.get(key) and pat.search(text):
            listing[key] = True


# --------------------------------------------------------------------------
# SCRAPE
# --------------------------------------------------------------------------
def scrape_combo(transaction: str, otodom_type: str, out_type: str):
    print(f"  🔎 {transaction}/{otodom_type} …")
    listings = []
    base = f"https://www.otodom.pl/pl/wyniki/{transaction}/{otodom_type}/cala-polska"
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}?limit=72&by=LATEST&direction=DESC&page={page}"
        data = fetch_page(url)
        if not data:
            print(f"    ✗ page {page} FAILED")
            break
        try:
            items = data["props"]["pageProps"]["data"]["searchAds"]["items"] or []
        except (KeyError, TypeError):
            items = []
        if not items:
            print(f"    · page {page}: 0 items → koniec paginacji")
            break
        added = 0
        for it in items:
            t = transform(it, transaction, out_type)
            if t:
                listings.append(t)
                added += 1
        print(f"    ✓ page {page}: +{added}/{len(items)}")
        if page < MAX_PAGES:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    print(f"  → {transaction}/{otodom_type}: {len(listings)} listings")
    return listings


def scrape_voj_combo(transaction: str, otodom_type: str, out_type: str,
                     voj: str, market_slug: str = "", market_param: str = ""):
    listings = []
    base = f"https://www.otodom.pl/pl/wyniki/{transaction}/{otodom_type}/{voj}"
    market_qs = f"&market={market_param}" if market_param else ""
    empty_streak = 0
    for page in range(1, VOJ_MAX_PAGES + 1):
        url = f"{base}?limit=72&by=LATEST&direction=DESC{market_qs}&page={page}"
        data = fetch_page(url)
        if not data:
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue
        try:
            items = data["props"]["pageProps"]["data"]["searchAds"]["items"] or []
        except (KeyError, TypeError):
            items = []
        if not items:
            break
        empty_streak = 0
        for it in items:
            t = transform(it, transaction, out_type)
            if t:
                if market_slug:
                    t["market_type"] = market_slug
                listings.append(t)
        if page < VOJ_MAX_PAGES:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    return listings


def scrape_voivodeships():
    all_voj_listings = []
    total = len(VOIVODESHIPS) * len(VOJ_COMBOS)
    idx = 0
    for voj in VOIVODESHIPS:
        for combo in VOJ_COMBOS:
            transaction, otodom_type, out_type, market_slug, market_param = combo
            idx += 1
            mk = f"/{market_slug}" if market_slug else ""
            print(f"  🗺️  [{idx}/{total}] {voj}/{transaction}/{otodom_type}{mk}", end=" ", flush=True)
            got = scrape_voj_combo(transaction, otodom_type, out_type, voj, market_slug, market_param)
            print(f"→ {len(got)}")
            all_voj_listings.extend(got)
    return all_voj_listings


# --------------------------------------------------------------------------
# DETAIL FETCH
# --------------------------------------------------------------------------
BUILDING_TYPE_MAP = {
    "block": "blok",
    "tenement": "kamienica",
    "apartment": "apartamentowiec",
    "loft": "loft",
    "highrise": "wiezowiec",
    "detached": "wolnostojacy",
    "semi_detached": "blizniak",
    "terraced": "szeregowiec",
    "ribbon": "szeregowiec",
    "chalet": "letniskowy",
    "farm": "siedliskowy",
    "residence": "rezydencja",
}
HEATING_MAP = {"urban": "miejskie", "gas": "gazowe", "electrical": "elektryczne",
               "boiler_room": "kotlownia", "tiled_stove": "kaflowy", "other": "inne"}
MATERIAL_MAP = {"brick": "cegla", "concrete_plate": "wielka_plyta", "wood": "drewno",
                "breezeblock": "pustak", "concrete": "beton", "cellular_concrete": "gazobeton",
                "silikat": "silikat", "other": "inne"}
STANDARD_MAP = {"ready_to_use": "gotowe", "to_completion": "do_wykonczenia",
                "to_renovation": "do_remontu"}
MARKET_MAP = {"primary": "pierwotny", "secondary": "wtorny"}


def _parse_target_field(target: dict, key: str) -> str:
    v = target.get(key)
    if isinstance(v, list) and v:
        return str(v[0])
    if isinstance(v, (str, int)):
        return str(v)
    return ""


def fetch_detail(slug_or_url: str) -> dict:
    if slug_or_url.startswith("http"):
        url = slug_or_url
    else:
        url = f"https://www.otodom.pl/pl/oferta/{slug_or_url}"

    time.sleep(DETAIL_DELAY + random.uniform(0, 0.4))

    for attempt in range(1, DETAIL_MAX_RETRIES + 2):
        r = scraper_get(url, timeout=DETAIL_TIMEOUT)
        if r is None:
            if attempt < DETAIL_MAX_RETRIES + 1:
                time.sleep(2 * attempt + random.uniform(0, 1))
                continue
            return {}
        try:
            if r.status_code == 200:
                start = r.text.find(NEXT_MARKER)
                if start == -1:
                    return {}
                start += len(NEXT_MARKER)
                end = r.text.find("</script>", start)
                if end == -1:
                    return {}
                data = json.loads(r.text[start:end])
                ad = data.get("props", {}).get("pageProps", {}).get("ad", {})
                if not ad:
                    return {}
                target = ad.get("target", {}) or {}
                bt_raw = _parse_target_field(target, "Building_type")
                extras = target.get("Extras_types") or []
                if not isinstance(extras, list):
                    extras = []
                detail = {
                    "building_type": BUILDING_TYPE_MAP.get(bt_raw, bt_raw),
                    "building_type_raw": bt_raw,
                    "build_year": int(_parse_target_field(target, "Build_year") or 0) or None,
                    "building_floors": int(target.get("Building_floors_num") or 0) or None,
                    "material": MATERIAL_MAP.get(_parse_target_field(target, "Building_material"), ""),
                    "heating": HEATING_MAP.get(_parse_target_field(target, "Heating"), ""),
                    "standard": STANDARD_MAP.get(_parse_target_field(target, "Construction_status"), ""),
                    "market_type": MARKET_MAP.get(target.get("MarketType", ""), ""),
                    "has_balcony": "balcony" in extras,
                    "has_terrace": "terrace" in extras,
                    "has_garden": "garden" in extras,
                    "has_lift": "lift" in extras,
                    "has_basement": "basement" in extras,
                    "has_garage": "garage" in extras or "garage_space" in extras,
                    "has_parking": "parking" in extras or "outdoor_parking_space" in extras,
                    "has_ac": "airconditioning" in extras or "air_conditioning" in extras,
                    "security": target.get("Security_types") or [],
                    "media": target.get("Media_types") or [],
                    "vicinity": target.get("Vicinity_types") or [],
                }
                floor_str = _parse_target_field(target, "Floor_no")
                if floor_str:
                    fm = re.search(r"(\d+)", floor_str)
                    if fm:
                        detail["floor_no"] = int(fm.group(1))
                    elif "ground" in floor_str or "parter" in floor_str.lower():
                        detail["floor_no"] = 0
                    elif "cellar" in floor_str or "basement" in floor_str:
                        detail["floor_no"] = -1
                return detail
            elif r.status_code in (403, 429, 503, 500, 502, 504):
                if attempt < DETAIL_MAX_RETRIES + 1:
                    time.sleep(min(4 * (2 ** (attempt - 1)), 20) + random.uniform(1, 3))
                    continue
                return {}
            else:
                return {}
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            if attempt < DETAIL_MAX_RETRIES + 1:
                time.sleep(2 * attempt)
                continue
            return {}
    return {}


def fetch_details_parallel(listings):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    targets = [l for l in listings if l.get("source_url")]
    if DETAIL_ONLY_ORIGINALS:
        targets = [l for l in targets if l.get("is_original", True)]
    if len(targets) > DETAIL_MAX:
        targets = targets[:DETAIL_MAX]
    total = len(targets)
    if not total:
        return 0
    print(f"  🔍 Fetch details: {total} ofert × {DETAIL_WORKERS} wątków (delay ~{DETAIL_DELAY}s/req)…")
    success = 0
    consecutive_fails = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        futures = {ex.submit(fetch_detail, l["source_url"]): l for l in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            l = futures[fut]
            try:
                detail = fut.result()
            except Exception:
                detail = {}
            if detail:
                l.update(detail)
                if "floor_no" in detail and detail["floor_no"] is not None:
                    l["floor"] = detail["floor_no"]
                if detail.get("building_floors"):
                    l["max_floor"] = detail["building_floors"]
                if detail.get("build_year") and not l.get("year_built"):
                    l["year_built"] = detail["build_year"]
                success += 1
                consecutive_fails = 0
            else:
                consecutive_fails += 1
            if i % 100 == 0:
                pct = success * 100 // i
                print(f"    · {i}/{total} ({success} OK, {pct}%)")
            if consecutive_fails >= 100:
                print(f"    ⚠️  100 kolejnych failów — przerywam (rate limit / ban)")
                for f in futures:
                    f.cancel()
                break
    print(f"  ✅ Details fetched: {success}/{total} ({success * 100 // max(total,1)}%)")
    return success


# --------------------------------------------------------------------------
# AI VERDICT
# --------------------------------------------------------------------------
def analyze_prices(listings) -> None:
    groups = defaultdict(list)
    for l in listings:
        key = (normalize_city(l["city"]), l["type"], l["transaction"])
        if l["price_pm2"] > 0:
            groups[key].append(l["price_pm2"])

    medians = {k: int(statistics.median(v)) for k, v in groups.items() if len(v) >= 3}
    global_med = {}
    global_groups = defaultdict(list)
    for l in listings:
        if l["price_pm2"] > 0:
            global_groups[(l["type"], l["transaction"])].append(l["price_pm2"])
    for k, v in global_groups.items():
        if v:
            global_med[k] = int(statistics.median(v))

    for l in listings:
        key = (normalize_city(l["city"]), l["type"], l["transaction"])
        median = medians.get(key) or global_med.get((l["type"], l["transaction"]))
        if not median or l["price_pm2"] == 0:
            continue
        rcn = int(median * 0.94)
        if rcn <= 0:
            continue
        l["ai_offers_pm2"] = median
        l["ai_rcn_pm2"] = rcn
        delta_pct = round((l["price_pm2"] - rcn) / rcn * 100)

        if delta_pct <= -35 or delta_pct >= 80:
            l["ai_delta_pct"] = delta_pct
            l["verdict_badge"] = "outlier"
            l["verdict_text"] = "❓ SPRAWDŹ"
            l["verdict_full"] = "Cena nietypowa — zweryfikuj ogłoszenie ręcznie"
            continue

        l["ai_delta_pct"] = delta_pct
        if delta_pct <= -8:
            l["verdict_badge"] = "deal"
            l["verdict_text"] = "🔥 OKAZJA"
            l["verdict_full"] = f"OKAZJA · {abs(delta_pct)}% poniżej realnych transakcji"
        elif delta_pct >= 10:
            l["verdict_badge"] = "over"
            l["verdict_text"] = "⚠️ ZAWYŻONA"
            l["verdict_full"] = f"ZAWYŻONA o {delta_pct}% powyżej transakcji"
        else:
            l["verdict_badge"] = "normal"
            l["verdict_text"] = "✓ W NORMIE"
            l["verdict_full"] = "CENA ZGODNA Z RYNKIEM"


# --------------------------------------------------------------------------
# DEDUPLICATE
# --------------------------------------------------------------------------
def deduplicate(listings):
    groups = defaultdict(list)
    for l in listings:
        fp = f"{normalize_city(l['city'])}|{l['type']}|{l['transaction']}|{int(l['price']/1000)*1000}|{int(l['area_m2'])}|{l['rooms']}"
        l["_fingerprint"] = fp
        groups[fp].append(l)

    originals = 0
    duplicates = 0
    for fp, group in groups.items():
        if len(group) == 1:
            group[0]["is_original"] = True
            group[0]["duplicate_of"] = None
            originals += 1
            continue
        group.sort(key=lambda x: (x.get("added_at") or "9999", -len(x.get("title") or "")))
        orig = group[0]
        orig["is_original"] = True
        orig["duplicate_of"] = None
        originals += 1
        for dup in group[1:]:
            dup["is_original"] = False
            dup["duplicate_of"] = orig["id"]
            duplicates += 1
    for l in listings:
        l.pop("_fingerprint", None)
    return originals, duplicates


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"🕷️  FinderDom Otodom scraper — start {started.isoformat()}")
    print(f"    MAX_PAGES={MAX_PAGES}  MAX_LISTINGS={MAX_LISTINGS}  DELAY={DELAY_MIN}-{DELAY_MAX}s")
    print(f"    OUT_FILE={OUT_FILE}  SKIP_DETAILS={SKIP_DETAILS}  DETAILS_ONLY={DETAILS_ONLY}")
    if SCRAPER_API_ENABLED:
        masked = SCRAPER_API_KEY[:6] + "…" + SCRAPER_API_KEY[-4:] if len(SCRAPER_API_KEY) > 12 else "***"
        print(f"    🌐 ScraperAPI: ENABLED  key={masked}  country={SCRAPER_API_COUNTRY}  premium={SCRAPER_API_PREMIUM}")
    else:
        print(f"    ⚠️  ScraperAPI: DISABLED (brak SCRAPER_API_KEY) — używam bezpośrednich requestów (ryzyko banu)")

    if DETAILS_ONLY:
        if not os.path.exists(OUT_FILE):
            print(f"❌ DETAILS_ONLY=1 ale brak pliku {OUT_FILE} — najpierw uruchom scrape!")
            return 1
        with open(OUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        all_listings = existing.get("listings", [])
        print(f"📂 Wczytano {len(all_listings)} ofert z {OUT_FILE}")

        detail_count = 0
        if FETCH_DETAILS and all_listings:
            print(f"\n🔬 Pobieranie szczegółów ofert (max {DETAIL_MAX})…")
            detail_count = fetch_details_parallel(all_listings)
        print("\n📝 Title parsing — inferuje z tytułów…")
        for l in all_listings:
            parse_title_hints(l)

        out = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(all_listings),
            "originals": existing.get("originals", 0),
            "duplicates": existing.get("duplicates", 0),
            "verdicts": existing.get("verdicts", {}),
            "per_combo": existing.get("per_combo", {}),
            "details_enriched": detail_count,
            "listings": all_listings,
        }
        os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"\n✅ Zapisano {OUT_FILE} (details_enriched={detail_count})")
        return 0

    all_listings = []
    stats_per_combo = {}
    for i, (transaction, otodom_type, out_type) in enumerate(COMBOS, 1):
        print(f"\n[{i}/{len(COMBOS)}] {transaction} / {otodom_type}")
        got = scrape_combo(transaction, otodom_type, out_type)
        stats_per_combo[f"{transaction}/{otodom_type}"] = len(got)
        all_listings.extend(got)
        if len(all_listings) >= MAX_LISTINGS:
            print(f"  ⚠️  hit MAX_LISTINGS={MAX_LISTINGS} — przerywam")
            break
        if i < len(COMBOS):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if len(all_listings) > MAX_LISTINGS:
        all_listings = all_listings[:MAX_LISTINGS]

    print(f"\n📊 Faza 1 (LATEST cała PL): {len(all_listings)} ofert")

    if SCRAPE_VOIVODESHIPS:
        print(f"\n🗺️  Faza 1b: {len(VOIVODESHIPS)} województw × {len(VOJ_COMBOS)} kombinacji "
              f"(max {VOJ_MAX_PAGES} stron each)")
        voj_listings = scrape_voivodeships()
        print(f"   → {len(voj_listings)} ofert ze wszystkich województw")
        all_listings.extend(voj_listings)
        if len(all_listings) > MAX_LISTINGS:
            print(f"   ⚠️  Powyżej MAX_LISTINGS={MAX_LISTINGS}, obcinam do limitu")
            all_listings = all_listings[:MAX_LISTINGS]

    print(f"\n📊 Zescrapowano łącznie: {len(all_listings)} ofert")
    print(f"    Breakdown: {stats_per_combo}")

    if len(all_listings) < MIN_LISTINGS:
        print(f"\n❌ Za mało ofert ({len(all_listings)} < {MIN_LISTINGS}). "
              f"NIE nadpisuję listings.json.")
        return 1

    print("\n🧮 Analiza cen (AI verdict)…")
    analyze_prices(all_listings)
    print("🔁 Deduplikacja…")
    originals, dupes = deduplicate(all_listings)
    print(f"    → {originals} oryginałów, {dupes} kopii")

    detail_count = 0
    if FETCH_DETAILS and not SKIP_DETAILS:
        print("\n🔬 Pobieranie szczegółów ofert (rok, typ budynku, standard, ogrzewanie, ...)")
        detail_count = fetch_details_parallel(all_listings)
    elif SKIP_DETAILS:
        print("\n⏭️  SKIP_DETAILS=1 — pomijam Fazę 2 (uruchom osobny workflow details-only)")

    print("\n📝 Title parsing — inferuje building_type/standard/features z tytułów…")
    hint_before = sum(1 for l in all_listings if l.get("building_type"))
    for l in all_listings:
        parse_title_hints(l)
    hint_after = sum(1 for l in all_listings if l.get("building_type"))
    print(f"   → building_type: {hint_before} (z detali) → {hint_after} (z tytułów) = +{hint_after - hint_before}")

    order = {"deal": 0, "normal": 1, "over": 2, "outlier": 3}
    all_listings.sort(key=lambda x: (
        not x.get("is_original", True),
        order.get(x.get("verdict_badge", "normal"), 1),
        x.get("ai_delta_pct", 0),
    ))

    if OUT_FILE.exists():
        try:
            BACKUP_FILE.write_bytes(OUT_FILE.read_bytes())
            print(f"💾 Backup: {BACKUP_FILE.name}")
        except OSError as e:
            print(f"⚠️  Nie zapisano backupu: {e}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    duration_s = int((finished - started).total_seconds())

    verdict_counts = defaultdict(int)
    for l in all_listings:
        verdict_counts[l["verdict_badge"]] += 1

    output = {
        "generated_at": finished.isoformat(),
        "generated_by": "otodom_scraper_v2",
        "duration_seconds": duration_s,
        "count": len(all_listings),
        "originals": originals,
        "duplicates": dupes,
        "sources": ["Otodom"],
        "verdicts": dict(verdict_counts),
        "per_combo": stats_per_combo,
        "details_fetched": detail_count,
        "note": "REAL DATA — scraped from Otodom.pl by GitHub Actions",
        "listings": all_listings,
    }
    OUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    size_mb = OUT_FILE.stat().st_size / 1024 / 1024
    print(f"\n✅ Zapisano {len(all_listings)} ofert → {OUT_FILE} ({size_mb:.2f} MB)")
    print(f"   Werdykty: {dict(verdict_counts)}")
    print(f"   Czas: {duration_s}s")
    return 0
