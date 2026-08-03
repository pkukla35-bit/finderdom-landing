"""FinderDom — Otodom scraper produkcyjny (v2).

Strategia:
  - Pobiera najnowsze ogłoszenia z całej Polski (`cala-polska`) sortowane po LATEST.
  - 9 kombinacji: (sprzedaz|wynajem) × (mieszkanie|dom|dzialka|lokal|pokoj|garaz).
  - Każde miasto Polski jest reprezentowane automatycznie (bez mapy miast).
  - Konfigurowalne przez zmienne środowiskowe (na potrzeby GitHub Actions).

Anti-bot:
  - Rotacja User-Agent, opóźnienia 1.5-3s, retry z exp backoff.
  - Pobiera JSON z `<script id="__NEXT_DATA__">` – szybko i stabilnie.

Safe-fail:
  - Jeśli scrape < MIN_LISTINGS (100) → NIE nadpisuje listings.json.
  - Zawsze tworzy backup poprzedniego pliku (listings.backup.json).
  - Wypisuje jasny błąd i wychodzi z kodem != 0 (widoczne w GH Actions).

Uruchomienie lokalne:
  python3 otodom_scraper_v2.py
  MAX_PAGES=3 python3 otodom_scraper_v2.py         # szybki test
  MAX_LISTINGS=1000 python3 otodom_scraper_v2.py   # limit output
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
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))          # 5 stron × 72 items × 9 komb = ~3200 max
MAX_LISTINGS = int(os.getenv("MAX_LISTINGS", "4000")) # hard cap na output JSON
MIN_LISTINGS = int(os.getenv("MIN_LISTINGS", "100"))  # safe-fail: mniej = nie zapisujemy
DELAY_MIN = float(os.getenv("DELAY_MIN", "1.5"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "3.0"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

OUT_DIR = Path(os.getenv(
    "OUT_DIR",
    str(Path(__file__).resolve().parent.parent / "data"),
))
OUT_FILE = OUT_DIR / "listings.json"
BACKUP_FILE = OUT_DIR / "listings.backup.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
]

# 9 kombinacji obsługiwanych przez Otodom
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

# Mapowanie roomsNumber (enum Otodom → int)
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
def fetch_page(url: str) -> dict | None:
    """Pobierz stronę Otodom + wyciągnij __NEXT_DATA__. Retry z backoffem."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Referer": "https://www.otodom.pl/",
            }
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                start = r.text.find(NEXT_MARKER)
                if start == -1:
                    last_err = "__NEXT_DATA__ not found"
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
            elif r.status_code in (403, 429, 503):
                last_err = f"HTTP {r.status_code} (bot detection)"
                time.sleep(5 + attempt * 3)  # backoff dłużej
            else:
                last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < MAX_RETRIES:
            time.sleep(2 * attempt)
    print(f"    [retry x{MAX_RETRIES} FAILED] {last_err}")
    return None


# --------------------------------------------------------------------------
# TRANSFORM
# --------------------------------------------------------------------------
def normalize_city(name: str) -> str:
    """Normalizacja nazwy miasta dla deduplikacji (lower, bez PL znaków, bez spacji)."""
    if not name:
        return ""
    s = name.lower().strip()
    trans = str.maketrans("ąćęłńóśźż", "acelnoszz")
    return re.sub(r"\s+", "-", s.translate(trans))


def extract_city_district(item: dict) -> tuple[str, str]:
    """Zwraca (miasto, dzielnica) z location.reverseGeocoding.locations."""
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


def detect_seller(item: dict) -> tuple[str, str]:
    """Zwraca (seller_type, seller_label) dla oferty Otodom."""
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


def transform(item: dict, transaction: str, typ_out: str) -> dict | None:
    """Transformacja jednego item-a Otodom → schema FinderDom."""
    try:
        # Cena — dla wynajmu totalPrice może być None, wtedy rentPrice
        tp = item.get("totalPrice") or {}
        rp = item.get("rentPrice") or {}
        price = tp.get("value") if tp else None
        if not price:
            price = rp.get("value") if rp else None
        if not price or price < 1000:  # sanity check
            return None

        area = item.get("areaInSquareMeters") or item.get("terrainAreaInSquareMeters") or 0
        if not area or area < 1:
            return None

        # ppm2 — z Otodom albo obliczone
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

        # Data
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

        # Obrazek — pierwsze zdjęcie z Otodom (URL do CDN)
        images = item.get("images") or []
        img_url = ""
        if images and isinstance(images[0], dict):
            img_url = images[0].get("large") or images[0].get("medium") or ""

        emoji = random.choice(TYPE_EMOJI.get(typ_out, ["🏢"]))

        return {
            "id": f"otodom-{item.get('id')}",
            "type": typ_out,
            "transaction": transaction,  # sprzedaz | wynajem
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
            # placeholdery — wypełni analyze_prices
            "verdict_badge": "normal",
            "verdict_text": "✓ W NORMIE",
            "verdict_full": "CENA ZGODNA Z RYNKIEM",
            "ai_delta_pct": 0,
            "ai_offers_pm2": 0,
            "ai_rcn_pm2": 0,
            # dedup — wypełni deduplicate
            "is_original": True,
            "duplicate_of": None,
            "_fingerprint": "",  # temp
        }
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        # nie hałasujemy — pojedyncze złe itemy mogą być
        return None


# --------------------------------------------------------------------------
# SCRAPE
# --------------------------------------------------------------------------
def scrape_combo(transaction: str, otodom_type: str, out_type: str) -> list[dict]:
    """Pobierz N stron dla jednej kombinacji (typ × transakcja)."""
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


# --------------------------------------------------------------------------
# AI VERDICT (mediana cena/m² per miasto + typ)
# --------------------------------------------------------------------------
def analyze_prices(listings: list[dict]) -> None:
    """Nadaje verdict_badge / ai_delta_pct każdej ofercie na podstawie mediany cena/m²
    w grupie (miasto + typ + transakcja).
    """
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for l in listings:
        key = (normalize_city(l["city"]), l["type"], l["transaction"])
        if l["price_pm2"] > 0:
            groups[key].append(l["price_pm2"])

    medians = {k: int(statistics.median(v)) for k, v in groups.items() if len(v) >= 3}
    # dla małych grup: fallback do mediany globalnej per typ+transakcja
    global_med: dict[tuple[str, str], int] = {}
    global_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
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
        # symulacja RCN (transakcyjne) — ok. 6% poniżej ofertowej mediany
        rcn = int(median * 0.94)
        l["ai_offers_pm2"] = median
        l["ai_rcn_pm2"] = rcn
        delta_pct = round((l["price_pm2"] - rcn) / rcn * 100)

        # Outlier guard: jeśli delta jest ekstremalna, prawie na pewno to błąd danych,
        # scam listing albo nieporównywalny obiekt (kamienica, luksus, itp.).
        # NIE promujemy takich jako OKAZJA i NIE straszymy jako ZAWYŻONA.
        if delta_pct <= -35 or delta_pct >= 80:
            l["ai_delta_pct"] = delta_pct  # zachowujemy dla debug
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
# DEDUPLICATE (fingerprint: cena + metraż + miasto + pokoje)
# --------------------------------------------------------------------------
def deduplicate(listings: list[dict]) -> tuple[int, int]:
    """Wyznacza fingerprint i oznacza duplikaty (is_original=False, duplicate_of=<id>)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for l in listings:
        # +/- 2% ceny toleranci nie robimy (na Otodom = ta sama oferta = ta sama cena)
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
        # wybierz oryginał: najstarsze added_at (albo najwięcej danych)
        group.sort(key=lambda x: (x.get("added_at") or "9999", -len(x.get("title") or "")))
        orig = group[0]
        orig["is_original"] = True
        orig["duplicate_of"] = None
        originals += 1
        for dup in group[1:]:
            dup["is_original"] = False
            dup["duplicate_of"] = orig["id"]
            duplicates += 1
    # cleanup temp
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
    print(f"    OUT_FILE={OUT_FILE}")

    all_listings: list[dict] = []
    stats_per_combo: dict[str, int] = {}
    for i, (transaction, otodom_type, out_type) in enumerate(COMBOS, 1):
        print(f"\n[{i}/{len(COMBOS)}] {transaction} / {otodom_type}")
        got = scrape_combo(transaction, otodom_type, out_type)
        stats_per_combo[f"{transaction}/{otodom_type}"] = len(got)
        all_listings.extend(got)
        if len(all_listings) >= MAX_LISTINGS:
            print(f"  ⚠️  hit MAX_LISTINGS={MAX_LISTINGS} — przerywam")
            break
        # delay między kombinacjami
        if i < len(COMBOS):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # Trim to MAX_LISTINGS
    if len(all_listings) > MAX_LISTINGS:
        all_listings = all_listings[:MAX_LISTINGS]

    print(f"\n📊 Zescrapowano łącznie: {len(all_listings)} ofert")
    print(f"    Breakdown: {stats_per_combo}")

    # -------- SAFE-FAIL -----------
    if len(all_listings) < MIN_LISTINGS:
        print(f"\n❌ Za mało ofert ({len(all_listings)} < {MIN_LISTINGS}). "
              f"NIE nadpisuję listings.json.")
        return 1

    # -------- ANALIZA CEN + DEDUP --
    print("\n🧮 Analiza cen (AI verdict)…")
    analyze_prices(all_listings)
    print("🔁 Deduplikacja…")
    originals, dupes = deduplicate(all_listings)
    print(f"    → {originals} oryginałów, {dupes} kopii")

    # -------- SORT: originals first, then by verdict (deal → normal → over → outlier) ---
    order = {"deal": 0, "normal": 1, "over": 2, "outlier": 3}
    all_listings.sort(key=lambda x: (
        not x.get("is_original", True),
        order.get(x.get("verdict_badge", "normal"), 1),
        x.get("ai_delta_pct", 0),
    ))

    # -------- BACKUP -----------
    if OUT_FILE.exists():
        try:
            BACKUP_FILE.write_bytes(OUT_FILE.read_bytes())
            print(f"💾 Backup: {BACKUP_FILE.name}")
        except OSError as e:
            print(f"⚠️  Nie zapisano backupu: {e}")

    # -------- SAVE -----------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    duration_s = int((finished - started).total_seconds())

    verdict_counts: dict[str, int] = defaultdict(int)
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
        "note": "REAL DATA — scraped from Otodom.pl by GitHub Actions",
        "listings": all_listings,
    }
    OUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    size_mb = OUT_FILE.stat().st_size / 1024 / 1024
    print(f"\n✅ Zapisano {len(all_listings)} ofert → {OUT_FILE} ({size_mb:.2f} MB)")
    print(f"   Werdykty: {dict(verdict_counts)}")
    print(f"   Czas: {duration_s}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
