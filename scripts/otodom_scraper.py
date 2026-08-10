"""FinderDom — Otodom scraper z ZenRows + ScraperAPI fallback + filtr po typie."""
from __future__ import annotations
import json, os, random, re, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("[FATAL] requests not installed")
    sys.exit(2)

MAX_PAGES = int(os.getenv("MAX_PAGES", "8"))
MAX_LISTINGS = int(os.getenv("MAX_LISTINGS", "60000"))
MIN_LISTINGS = int(os.getenv("MIN_LISTINGS", "100"))
DELAY_MIN = float(os.getenv("DELAY_MIN", "1.2"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "2.5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
SCRAPER_API_ENABLED = bool(SCRAPER_API_KEY)
SCRAPER_API_PREMIUM = os.getenv("SCRAPER_API_PREMIUM", "1") == "1"
SCRAPER_API_COUNTRY = os.getenv("SCRAPER_API_COUNTRY", "pl")
SCRAPER_API_ENDPOINT = "http://api.scraperapi.com/"

ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "").strip()
ZENROWS_ENABLED = bool(ZENROWS_API_KEY)
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
ZENROWS_ANTIBOT = os.getenv("ZENROWS_ANTIBOT", "1") == "1"

FETCH_DETAILS = os.getenv("FETCH_DETAILS", "1") == "1"
SKIP_DETAILS = os.getenv("SKIP_DETAILS", "0") == "1"
DETAILS_ONLY = os.getenv("DETAILS_ONLY", "0") == "1"
DETAIL_WORKERS = int(os.getenv("DETAIL_WORKERS", "6"))
DETAIL_TIMEOUT = int(os.getenv("DETAIL_TIMEOUT", "15"))
DETAIL_ONLY_ORIGINALS = os.getenv("DETAIL_ONLY_ORIGINALS", "1") == "1"
DETAIL_ONLY_TYPE = os.getenv("DETAIL_ONLY_TYPE", "").strip()
DETAIL_SKIP_ENRICHED = os.getenv("DETAIL_SKIP_ENRICHED", "1") == "1"
DETAIL_MAX = int(os.getenv("DETAIL_MAX", "60000"))
DETAIL_DELAY = float(os.getenv("DETAIL_DELAY", "0.3"))
DETAIL_MAX_RETRIES = int(os.getenv("DETAIL_MAX_RETRIES", "2"))

SCRAPE_VOIVODESHIPS = os.getenv("SCRAPE_VOIVODESHIPS", "1") == "1"
VOJ_MAX_PAGES = int(os.getenv("VOJ_MAX_PAGES", "20"))

VOIVODESHIPS = ["dolnoslaskie","kujawsko--pomorskie","lubelskie","lubuskie","lodzkie","malopolskie","mazowieckie","opolskie","podkarpackie","podlaskie","pomorskie","slaskie","swietokrzyskie","warminsko--mazurskie","wielkopolskie","zachodniopomorskie"]

VOJ_COMBOS = [
    ("sprzedaz","mieszkanie","mieszkanie","pierwotny","PRIMARY"),
    ("sprzedaz","mieszkanie","mieszkanie","wtorny","SECONDARY"),
    ("sprzedaz","dom","dom","pierwotny","PRIMARY"),
    ("sprzedaz","dom","dom","wtorny","SECONDARY"),
    ("sprzedaz","dzialka","dzialka","",""),
    ("sprzedaz","lokal","lokal","pierwotny","PRIMARY"),
    ("sprzedaz","lokal","lokal","wtorny","SECONDARY"),
    ("wynajem","mieszkanie","mieszkanie","",""),
    ("wynajem","dom","dom","",""),
]

OUT_DIR = Path(os.getenv("OUT_DIR", str(Path(__file__).resolve().parent.parent / "data")))
OUT_FILE = OUT_DIR / "listings.json"
BACKUP_FILE = OUT_DIR / "listings.backup.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

COMBOS = [
    ("sprzedaz","mieszkanie","mieszkanie"),
    ("sprzedaz","dom","dom"),
    ("sprzedaz","dzialka","dzialka"),
    ("sprzedaz","lokal","lokal"),
    ("sprzedaz","garaz","garaz"),
    ("wynajem","mieszkanie","mieszkanie"),
    ("wynajem","dom","dom"),
    ("wynajem","pokoj","pokoj"),
    ("wynajem","lokal","lokal"),
]

ROOMS_ENUM = {"ONE":1,"TWO":2,"THREE":3,"FOUR":4,"FIVE":5,"SIX":6,"SEVEN":7,"EIGHT":8,"NINE":9,"MORE":10,"TEN_AND_MORE":10}
TYPE_EMOJI = {"mieszkanie":["🏢"],"dom":["🏠"],"dzialka":["🌳"],"lokal":["🏬"],"pokoj":["🚪"],"garaz":["🚗"]}
IMG_CLASSES = ["i1","i2","i3","i4","i5","i6"]
NEXT_MARKER = '<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'

BUILDING_TYPE_MAP = {"block":"blok","tenement":"kamienica","apartment":"apartamentowiec","loft":"loft","highrise":"wiezowiec","detached":"wolnostojacy","semi_detached":"blizniak","terraced":"szeregowiec","ribbon":"szeregowiec","chalet":"letniskowy","farm":"siedliskowy","residence":"rezydencja"}
HEATING_MAP = {"urban":"miejskie","gas":"gazowe","electrical":"elektryczne","boiler_room":"kotlownia","tiled_stove":"kaflowy","other":"inne"}
MATERIAL_MAP = {"brick":"cegla","concrete_plate":"wielka_plyta","wood":"drewno","breezeblock":"pustak","concrete":"beton","cellular_concrete":"gazobeton","silikat":"silikat","other":"inne"}
STANDARD_MAP = {"ready_to_use":"gotowe","to_completion":"do_wykonczenia","to_renovation":"do_remontu"}
MARKET_MAP = {"primary":"pierwotny","secondary":"wtorny"}

BUILDING_TYPE_KEYWORDS = [
    (re.compile(r"\b(bli[żz]niak|pol[oó]w[ka]?\s+dom)", re.I), "blizniak"),
    (re.compile(r"\b(szeregow|segment|w\s+szereg)", re.I), "szeregowiec"),
    (re.compile(r"\b(siedlisk|gospodarstw|zagrod)", re.I), "siedliskowy"),
    (re.compile(r"\b(letnisk|weekend|domek\s+let)", re.I), "letniskowy"),
    (re.compile(r"\b(rezydencj|pa[łl]ac|willa|villa)", re.I), "rezydencja"),
    (re.compile(r"\bwolnostoj|wolno.?stoj", re.I), "wolnostojacy"),
    (re.compile(r"\bapartament(?!ow)", re.I), "apartamentowiec"),
    (re.compile(r"\bkamienic", re.I), "kamienica"),
    (re.compile(r"\bblok\b|w\s+bloku", re.I), "blok"),
]
STANDARD_KEYWORDS = [
    (re.compile(r"\bdo\s+remontu", re.I), "do_remontu"),
    (re.compile(r"\bdo\s+wyko[nń]czenia|deweloperski", re.I), "do_wykonczenia"),
    (re.compile(r"\bdo\s+zamieszkania|wyko[nń]czone|po\s+remoncie", re.I), "gotowe"),
]
BOOL_KEYWORDS = {
    "has_balcony": re.compile(r"\bbalkon", re.I),
    "has_terrace": re.compile(r"\btaras", re.I),
    "has_garden": re.compile(r"\bogr[oó]d", re.I),
    "has_lift": re.compile(r"\bwinda", re.I),
    "has_garage": re.compile(r"\bgara[żz]", re.I),
}


def scraper_get(url, timeout=REQUEST_TIMEOUT):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl-PL,pl;q=0.9",
        "Referer": "https://www.otodom.pl/",
    }
    try:
        if ZENROWS_ENABLED:
            params = {"apikey": ZENROWS_API_KEY, "url": url, "premium_proxy": "true", "proxy_country": "pl"}
            if ZENROWS_ANTIBOT:
                params["antibot"] = "true"
            return requests.get(ZENROWS_ENDPOINT, params=params, timeout=max(timeout, 90))
        if SCRAPER_API_ENABLED:
            params = {"api_key": SCRAPER_API_KEY, "url": url, "country_code": SCRAPER_API_COUNTRY, "keep_headers": "true"}
            if SCRAPER_API_PREMIUM:
                params["ultra_premium"] = "true"
            return requests.get(SCRAPER_API_ENDPOINT, params=params, headers=headers, timeout=max(timeout, 70))
        return requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException:
        return None


def fetch_page(url):
    for attempt in range(1, MAX_RETRIES + 1):
        r = scraper_get(url, timeout=REQUEST_TIMEOUT)
        if r is not None and r.status_code == 200:
            s = r.text.find(NEXT_MARKER)
            if s != -1:
                s += len(NEXT_MARKER)
                e = r.text.find("</script>", s)
                if e != -1:
                    try:
                        return json.loads(r.text[s:e])
                    except json.JSONDecodeError:
                        pass
        if attempt < MAX_RETRIES:
            time.sleep(min(4 * (2 ** (attempt - 1)), 32) + random.uniform(0, 2))
    return None


def normalize_city(name):
    if not name:
        return ""
    s = name.lower().strip()
    return re.sub(r"\s+", "-", s.translate(str.maketrans("ąćęłńóśźż", "acelnoszz")))


def extract_city_district(item):
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


def detect_seller(item):
    if item.get("isPrivateOwner"):
        return "prywatna", "Prywatna"
    if item.get("isDeveloperOwner"):
        return "deweloper", "Deweloper"
    adv = (item.get("extendedAdvertiserType") or "").upper()
    if adv == "PRIVATE":
        return "prywatna", "Prywatna"
    if adv == "DEVELOPER":
        return "deweloper", "Deweloper"
    return "posrednik", "Posrednik"


def transform(item, transaction, typ_out):
    try:
        tp = item.get("totalPrice") or {}
        rp = item.get("rentPrice") or {}
        price = (tp.get("value") if tp else None) or (rp.get("value") if rp else None)
        if not price or price < 1000:
            return None
        area = item.get("areaInSquareMeters") or item.get("terrainAreaInSquareMeters") or 0
        if not area or area < 1:
            return None
        pm2 = item.get("pricePerSquareMeter") or {}
        ppm2 = int(pm2.get("value") or 0) if pm2 else 0
        if not ppm2 and area > 0:
            ppm2 = int(price / area)
        rooms_raw = item.get("roomsNumber")
        rooms = ROOMS_ENUM.get(rooms_raw, 0) if isinstance(rooms_raw, str) else (int(rooms_raw) if isinstance(rooms_raw, (int, float)) else 0)
        city, district = extract_city_district(item)
        if not city:
            return None
        seller_type, seller_label = detect_seller(item)
        _loc = item.get("location") or {}
        _coords = _loc.get("coordinates") or {}
        _lat = _coords.get("latitude")
        _lon = _coords.get("longitude")
        slug = item.get("slug", "")
        source_url = f"https://www.otodom.pl/pl/oferta/{slug}" if slug else ""
        title = (item.get("title") or "")[:120]
        images = item.get("images") or []
        img_url = images[0].get("large") if images and isinstance(images[0], dict) else ""
        return {
            "id": f"otodom-{item.get('id')}",
            "type": typ_out, "transaction": transaction, "title": title,
            "city": city, "district": district,
            "location": f"{city}{', ' + district if district else ''}",
            "area_m2": round(float(area), 1), "rooms": rooms, "floor": 0, "max_floor": 0,
            "year_built": 0, "standard": "", "price": int(price), "price_pm2": ppm2,
            "price_display": f"{int(price):,} zl".replace(",", " "),
            "price_pm2_display": f"{ppm2:,} zl/m2".replace(",", " "),
            "portal": "Otodom", "seller_type": seller_type, "seller_label": seller_label,
            "source_url": source_url, "image_url": img_url or "",
            "emoji": random.choice(TYPE_EMOJI.get(typ_out, ["🏢"])),
            "img_class": random.choice(IMG_CLASSES),
            "added_at": item.get("dateCreated", ""), "added_display": "",
            "verdict_badge": "normal", "verdict_text": "W NORMIE",
            "verdict_full": "CENA ZGODNA Z RYNKIEM",
            "ai_delta_pct": 0, "ai_offers_pm2": 0, "ai_rcn_pm2": 0,
            "is_original": True, "duplicate_of": None,
             "lat": _lat, "lon": _lon,
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def parse_title_hints(listing):
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


def scrape_combo(transaction, otodom_type, out_type):
    print(f"  {transaction}/{otodom_type}")
    listings = []
    base = f"https://www.otodom.pl/pl/wyniki/{transaction}/{otodom_type}/cala-polska"
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}?limit=72&by=LATEST&direction=DESC&page={page}"
        data = fetch_page(url)
        if not data:
            break
        try:
            items = data["props"]["pageProps"]["data"]["searchAds"]["items"] or []
        except (KeyError, TypeError):
            items = []
        if not items:
            break
        for it in items:
            t = transform(it, transaction, out_type)
            if t:
                listings.append(t)
        if page < MAX_PAGES:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    return listings


def scrape_voj_combo(transaction, otodom_type, out_type, voj, market_slug="", market_param=""):
    listings = []
    base = f"https://www.otodom.pl/pl/wyniki/{transaction}/{otodom_type}/{voj}"
    market_qs = f"&market={market_param}" if market_param else ""
    for page in range(1, VOJ_MAX_PAGES + 1):
        url = f"{base}?limit=72&by=LATEST&direction=DESC{market_qs}&page={page}"
        data = fetch_page(url)
        if not data:
            continue
        try:
            items = data["props"]["pageProps"]["data"]["searchAds"]["items"] or []
        except (KeyError, TypeError):
            items = []
        if not items:
            break
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
    out = []
    total = len(VOIVODESHIPS) * len(VOJ_COMBOS)
    idx = 0
    for voj in VOIVODESHIPS:
        for combo in VOJ_COMBOS:
            transaction, otodom_type, out_type, market_slug, market_param = combo
            idx += 1
            print(f"  [{idx}/{total}] {voj}/{transaction}/{otodom_type}", end=" ", flush=True)
            got = scrape_voj_combo(transaction, otodom_type, out_type, voj, market_slug, market_param)
            print(f"-> {len(got)}")
            out.extend(got)
    return out


def _parse_target_field(target, key):
    v = target.get(key)
    if isinstance(v, list) and v:
        return str(v[0])
    if isinstance(v, (str, int)):
        return str(v)
    return ""


def fetch_detail(slug_or_url):
    url = slug_or_url if slug_or_url.startswith("http") else f"https://www.otodom.pl/pl/oferta/{slug_or_url}"
    time.sleep(DETAIL_DELAY + random.uniform(0, 0.4))
    for attempt in range(1, DETAIL_MAX_RETRIES + 2):
        r = scraper_get(url, timeout=DETAIL_TIMEOUT)
        if r is None:
            if attempt < DETAIL_MAX_RETRIES + 1:
                time.sleep(2 * attempt)
                continue
            return {}
        try:
            if r.status_code == 200:
                s = r.text.find(NEXT_MARKER)
                if s == -1:
                    return {}
                s += len(NEXT_MARKER)
                e = r.text.find("</script>", s)
                if e == -1:
                    return {}
                data = json.loads(r.text[s:e])
                ad = data.get("props", {}).get("pageProps", {}).get("ad", {})
                if not ad:
                    return {}
                target = ad.get("target", {}) or {}
                bt_raw = _parse_target_field(target, "Building_type")
                extras = target.get("Extras_types") or []
                if not isinstance(extras, list):
                    extras = []
                return {
                    "building_type": BUILDING_TYPE_MAP.get(bt_raw, bt_raw),
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
                    "has_garage": "garage" in extras or "garage_space" in extras,
                }
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
    if DETAIL_ONLY_TYPE:
        targets = [l for l in targets if l.get("type") == DETAIL_ONLY_TYPE]
        print(f"  Filter: only type={DETAIL_ONLY_TYPE} ({len(targets)} pasujacych)")
    if DETAIL_SKIP_ENRICHED:
        targets = [l for l in targets if not l.get("material") and not l.get("heating")]
        print(f"  Skip enriched: {len(targets)} pozostalo (bez material/heating)")
    if len(targets) > DETAIL_MAX:
        targets = targets[:DETAIL_MAX]
    total = len(targets)
    if not total:
        print("  Brak ofert do wzbogacenia")
        return 0
    print(f"  Fetch details: {total} ofert x {DETAIL_WORKERS} watkow")
    success = 0
    fails = 0
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
                if detail.get("build_year") and not l.get("year_built"):
                    l["year_built"] = detail["build_year"]
                success += 1
                fails = 0
            else:
                fails += 1
            if i % 50 == 0:
                print(f"    {i}/{total} ({success} OK)")
            if fails >= 100:
                print(f"    100 kolejnych failow - przerywam")
                for f in futures:
                    f.cancel()
                break
    print(f"  Details: {success}/{total}")
    return success


def analyze_prices(listings):
    groups = defaultdict(list)
    for l in listings:
        if l["price_pm2"] > 0:
            groups[(normalize_city(l["city"]), l["type"], l["transaction"])].append(l["price_pm2"])
    medians = {k: int(statistics.median(v)) for k, v in groups.items() if len(v) >= 3}
    gg = defaultdict(list)
    for l in listings:
        if l["price_pm2"] > 0:
            gg[(l["type"], l["transaction"])].append(l["price_pm2"])
    gm = {k: int(statistics.median(v)) for k, v in gg.items() if v}
    for l in listings:
        k = (normalize_city(l["city"]), l["type"], l["transaction"])
        median = medians.get(k) or gm.get((l["type"], l["transaction"]))
        if not median or l["price_pm2"] == 0:
            continue
        rcn = int(median * 0.94)
        if rcn <= 0:
            continue
        l["ai_offers_pm2"] = median
        l["ai_rcn_pm2"] = rcn
        d = round((l["price_pm2"] - rcn) / rcn * 100)
        if d <= -35 or d >= 80:
            l["ai_delta_pct"] = d
            l["verdict_badge"] = "outlier"
            l["verdict_text"] = "SPRAWDZ"
            l["verdict_full"] = "Cena nietypowa"
            continue
        l["ai_delta_pct"] = d
        if d <= -8:
            l["verdict_badge"] = "deal"
            l["verdict_text"] = "OKAZJA"
            l["verdict_full"] = f"{abs(d)}% ponizej rynku"
        elif d >= 10:
            l["verdict_badge"] = "over"
            l["verdict_text"] = "ZAWYZONA"
            l["verdict_full"] = f"{d}% powyzej rynku"


def deduplicate(listings):
    groups = defaultdict(list)
    for l in listings:
        fp = f"{normalize_city(l['city'])}|{l['type']}|{l['transaction']}|{int(l['price']/1000)*1000}|{int(l['area_m2'])}|{l['rooms']}"
        groups[fp].append(l)
    orig, dup = 0, 0
    for _, group in groups.items():
        if len(group) == 1:
            group[0]["is_original"] = True
            orig += 1
            continue
        group.sort(key=lambda x: (x.get("added_at") or "9999"))
        group[0]["is_original"] = True
        orig += 1
        for d in group[1:]:
            d["is_original"] = False
            d["duplicate_of"] = group[0]["id"]
            dup += 1
    return orig, dup


def main():
    started = datetime.now(timezone.utc)
    print(f"Otodom scraper start {started.isoformat()}")
    print(f"    DETAILS_ONLY={DETAILS_ONLY} DETAIL_MAX={DETAIL_MAX} ONLY_TYPE={DETAIL_ONLY_TYPE!r}")
    if ZENROWS_ENABLED:
        print(f"    ZenRows: ENABLED antibot={ZENROWS_ANTIBOT}")
    elif SCRAPER_API_ENABLED:
        print(f"    ScraperAPI: ENABLED country={SCRAPER_API_COUNTRY}")
    else:
        print(f"    Proxy: DISABLED")

    if DETAILS_ONLY:
        if not os.path.exists(OUT_FILE):
            print(f"Brak {OUT_FILE}")
            return 1
        with open(OUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        all_listings = existing.get("listings", [])
        print(f"Wczytano {len(all_listings)} ofert")
        dc = 0
        if FETCH_DETAILS and all_listings:
            dc = fetch_details_parallel(all_listings)
        for l in all_listings:
            parse_title_hints(l)
        out = {**existing, "generated_at": datetime.now(timezone.utc).isoformat(),
               "details_enriched": (existing.get("details_enriched") or 0) + dc, "listings": all_listings}
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Zapisano (nowo_enriched={dc})")
        return 0

    all_listings = []
    stats = {}
    for i, (t, ot, out_t) in enumerate(COMBOS, 1):
        print(f"[{i}/{len(COMBOS)}] {t}/{ot}")
        got = scrape_combo(t, ot, out_t)
        stats[f"{t}/{ot}"] = len(got)
        all_listings.extend(got)
        if len(all_listings) >= MAX_LISTINGS:
            break
        if i < len(COMBOS):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if len(all_listings) > MAX_LISTINGS:
        all_listings = all_listings[:MAX_LISTINGS]

    if SCRAPE_VOIVODESHIPS:
        voj = scrape_voivodeships()
        all_listings.extend(voj)
        if len(all_listings) > MAX_LISTINGS:
            all_listings = all_listings[:MAX_LISTINGS]

    if len(all_listings) < MIN_LISTINGS:
        print(f"Za malo ({len(all_listings)} < {MIN_LISTINGS})")
        return 1

    analyze_prices(all_listings)
    orig, dup = deduplicate(all_listings)
    print(f"{orig} oryginalow, {dup} kopii")

    dc = 0
    if FETCH_DETAILS and not SKIP_DETAILS:
        dc = fetch_details_parallel(all_listings)

    for l in all_listings:
        parse_title_hints(l)

    order = {"deal": 0, "normal": 1, "over": 2, "outlier": 3}
    all_listings.sort(key=lambda x: (not x.get("is_original", True), order.get(x.get("verdict_badge", "normal"), 1), x.get("ai_delta_pct", 0)))

    if OUT_FILE.exists():
        try:
            BACKUP_FILE.write_bytes(OUT_FILE.read_bytes())
        except OSError:
            pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    duration_s = int((finished - started).total_seconds())
    vc = defaultdict(int)
    for l in all_listings:
        vc[l["verdict_badge"]] += 1

    output = {
        "generated_at": finished.isoformat(), "generated_by": "otodom_scraper_v5",
        "duration_seconds": duration_s, "count": len(all_listings),
        "originals": orig, "duplicates": dup, "sources": ["Otodom"],
        "verdicts": dict(vc), "per_combo": stats, "details_fetched": dc,
        "listings": all_listings,
    }
    OUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano {len(all_listings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
