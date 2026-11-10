#!/usr/bin/env python3
"""
Universal Otodom scraper via ScrapingBee — mieszkania / domy / działki.

Usage:
    export SCRAPINGBEE_API_KEY=xxx
    export MONGODB_URI="mongodb+srv://..."
    python scripts/scrape_otodom.py --property mieszkanie   # ~500 credits
    python scripts/scrape_otodom.py --property dom          # ~300 credits
    python scripts/scrape_otodom.py --property dzialka      # ~250 credits
    python scripts/scrape_otodom.py --property mieszkanie --city warszawa --max-pages 2
"""
from __future__ import annotations
import argparse, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, ASCENDING, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_type import infer_market  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("otodom")

SB_URL = "https://app.scrapingbee.com/api/v1/"

# 16 województw — tryb --regions (cała Polska, jak w Apify). "voi" = slug używany przez Otodom/Morizon/Gratka/Domiporta.
REGIONS: List[Dict[str, str]] = [{"name": n, "voi": v, "slug": v} for n, v in [
    ("dolnośląskie", "dolnoslaskie"), ("kujawsko-pomorskie", "kujawsko-pomorskie"), ("lubelskie", "lubelskie"),
    ("lubuskie", "lubuskie"), ("łódzkie", "lodzkie"), ("małopolskie", "malopolskie"), ("mazowieckie", "mazowieckie"),
    ("opolskie", "opolskie"), ("podkarpackie", "podkarpackie"), ("podlaskie", "podlaskie"), ("pomorskie", "pomorskie"),
    ("śląskie", "slaskie"), ("świętokrzyskie", "swietokrzyskie"), ("warmińsko-mazurskie", "warminsko-mazurskie"),
    ("wielkopolskie", "wielkopolskie"), ("zachodniopomorskie", "zachodniopomorskie")]]

TWO_WORD_CITIES = {"nowy-sacz": "Nowy Sącz", "bielsko-biala": "Bielsko-Biała", "zielona-gora": "Zielona Góra",
    "gorzow-wielkopolski": "Gorzów Wielkopolski", "jelenia-gora": "Jelenia Góra", "dabrowa-gornicza": "Dąbrowa Górnicza",
    "ruda-slaska": "Ruda Śląska", "piotrkow-trybunalski": "Piotrków Trybunalski", "ostrow-wielkopolski": "Ostrów Wielkopolski",
    "stalowa-wola": "Stalowa Wola", "tomaszow-mazowiecki": "Tomaszów Mazowiecki", "nowy-targ": "Nowy Targ",
    "nowy-dwor-mazowiecki": "Nowy Dwór Mazowiecki", "ostrowiec-swietokrzyski": "Ostrowiec Świętokrzyski",
    "skarzysko-kamienna": "Skarżysko-Kamienna", "mielec": "Mielec", "kedzierzyn-kozle": "Kędzierzyn-Koźle",
    "grodzisk-mazowiecki": "Grodzisk Mazowiecki", "minsk-mazowiecki": "Mińsk Mazowiecki", "tomaszow-lubelski": "Tomaszów Lubelski",
    "czechowice-dziedzice": "Czechowice-Dziedzice", "konstancin-jeziorna": "Konstancin-Jeziorna", "busko-zdroj": "Busko-Zdrój",
    "kudowa-zdroj": "Kudowa-Zdrój", "polanica-zdroj": "Polanica-Zdrój", "rabka-zdroj": "Rabka-Zdrój", "szczawno-zdroj": "Szczawno-Zdrój"}


def city_from_slug(tokens: List[str]) -> str:
    """Z tokenów sluga URL (po typie nieruchomości) odczytaj miasto: 2-wyrazowe ze słownika, inaczej pierwszy token."""
    if not tokens: return ""
    two = "-".join(tokens[:2])
    if two in TWO_WORD_CITIES: return TWO_WORD_CITIES[two]
    known = {c["slug"].split("/")[0]: c["name"] for c in CITIES}   # krakow → Kraków, lodz → Łódź …
    return known.get(tokens[0]) or tokens[0].capitalize()


CITIES: List[Dict[str, str]] = [
    {"name": "Warszawa",    "voi": "mazowieckie",         "slug": "warszawa/warszawa/warszawa"},
    {"name": "Kraków",      "voi": "malopolskie",         "slug": "krakow/krakow/krakow"},
    {"name": "Wrocław",     "voi": "dolnoslaskie",        "slug": "wroclaw/wroclaw/wroclaw"},
    {"name": "Łódź",        "voi": "lodzkie",             "slug": "lodz/lodz/lodz"},
    {"name": "Poznań",      "voi": "wielkopolskie",       "slug": "poznan/poznan/poznan"},
    {"name": "Gdańsk",      "voi": "pomorskie",           "slug": "gdansk/gdansk/gdansk"},
    {"name": "Szczecin",    "voi": "zachodniopomorskie",  "slug": "szczecin/szczecin/szczecin"},
    {"name": "Bydgoszcz",   "voi": "kujawsko--pomorskie", "slug": "bydgoszcz/bydgoszcz/bydgoszcz"},
    {"name": "Lublin",      "voi": "lubelskie",           "slug": "lublin/lublin/lublin"},
    {"name": "Białystok",   "voi": "podlaskie",           "slug": "bialystok/bialystok/bialystok"},
    {"name": "Katowice",    "voi": "slaskie",             "slug": "katowice/katowice/katowice"},
    {"name": "Gdynia",      "voi": "pomorskie",           "slug": "gdynia/gdynia/gdynia"},
    {"name": "Częstochowa", "voi": "slaskie",             "slug": "czestochowa/czestochowa/czestochowa"},
    {"name": "Radom",       "voi": "mazowieckie",         "slug": "radom/radom/radom"},
    {"name": "Toruń",       "voi": "kujawsko--pomorskie", "slug": "torun/torun/torun"},
    {"name": "Rzeszów",     "voi": "podkarpackie",        "slug": "rzeszow/rzeszow/rzeszow"},
    {"name": "Kielce",      "voi": "swietokrzyskie",      "slug": "kielce/kielce/kielce"},
    {"name": "Olsztyn",     "voi": "warminsko--mazurskie","slug": "olsztyn/olsztyn/olsztyn"},
    {"name": "Zabrze",      "voi": "slaskie",             "slug": "zabrze/zabrze/zabrze"},
    {"name": "Sosnowiec",   "voi": "slaskie",             "slug": "sosnowiec/sosnowiec/sosnowiec"},
]

PROPERTY_SLUGS = {
    "mieszkanie": "mieszkanie",
    "dom":        "dom",
    "dzialka":    "dzialka",
    "lokal":      "lokal",
}
PROPERTY_LABELS = {
    "mieszkanie": "mieszkanie",
    "dom":        "dom",
    "dzialka":    "działka",
    "lokal":      "lokal",
}


TRANSACTION = "sprzedaz"  # nadpisywane przez --transaction (sprzedaz | wynajem)


def otodom_url(prop: str, city: Dict[str, str], page: int) -> str:
    slug = PROPERTY_SLUGS[prop]
    if city["slug"] == city["voi"]:   # tryb --regions: całe województwo, od najnowszych
        return f"https://www.otodom.pl/pl/wyniki/{TRANSACTION}/{slug}/{city['voi']}?limit=72&page={page}&by=LATEST&direction=DESC"
    return f"https://www.otodom.pl/pl/wyniki/{TRANSACTION}/{slug}/{city['voi']}/{city['slug']}?limit=72&page={page}"


def fetch_via_scrapingbee(url: str, api_key: str) -> Optional[str]:
    params = {"api_key": api_key, "url": url, "render_js": "false",
              "premium_proxy": "false", "country_code": "pl"}
    try:
        r = requests.get(SB_URL, params=params, timeout=60)
        if r.status_code == 200:
            return r.text
        log.warning("SB HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("SB fetch error: %s", e)
    return None


# ── Tryb bezpośredni (0 zł): z GitHub Actions Otodom odpowiada bez proxy. Po 3 blokadach (403/429)
#    z rzędu przełączamy się na ScrapingBee (jeśli jest klucz), żeby import nigdy nie padł.
_DIRECT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
_DIRECT_HEADERS = {"Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
try:
    # curl_cffi podrabia odcisk TLS/HTTP2 Chrome — pierwsza linia obrony przed 403 (bez renderowania przeglądarki)
    from curl_cffi import requests as _creq  # type: ignore
    _DIRECT = _creq.Session(impersonate="chrome")
    _DIRECT_KIND = "curl_cffi"
except Exception:  # pragma: no cover
    _DIRECT = requests.Session()
    _DIRECT.headers.update({"User-Agent": _DIRECT_UA})
    _DIRECT_KIND = "requests"
DIRECT_STATE = {"blocked_streak": 0, "disabled": False, "direct_ok": 0, "sb_used": 0}


def fetch_direct(url: str) -> Optional[str]:
    try:
        r = _DIRECT.get(url, headers=_DIRECT_HEADERS, timeout=30)
        if r.status_code == 200 and "__NEXT_DATA__" in r.text:
            DIRECT_STATE["blocked_streak"] = 0; DIRECT_STATE["direct_ok"] += 1
            return r.text
        if r.status_code in (403, 429):
            DIRECT_STATE["blocked_streak"] += 1
            log.warning("direct HTTP %s (streak %d)", r.status_code, DIRECT_STATE["blocked_streak"])
            time.sleep(5)
        else:
            log.warning("direct HTTP %s", r.status_code)
    except Exception as e:
        log.warning("direct error: %s", str(e)[:100])
    return None


def fetch_page(url: str, api_key: Optional[str]) -> Optional[str]:
    """Najpierw bezpośrednio (darmowo); przy trwałej blokadzie — ScrapingBee."""
    if not DIRECT_STATE["disabled"]:
        html = fetch_direct(url)
        if html:
            return html
        if DIRECT_STATE["blocked_streak"] >= 3 and api_key:
            DIRECT_STATE["disabled"] = True
            log.warning("→ Otodom blokuje IP GitHuba — przełączam na ScrapingBee do końca uruchomienia")
        elif not api_key:
            return None
        else:
            # pojedynczy błąd: spróbuj tę stronę przez SB, ale nie wyłączaj trybu direct
            DIRECT_STATE["sb_used"] += 1
            return fetch_via_scrapingbee(url, api_key)
    DIRECT_STATE["sb_used"] += 1
    return fetch_via_scrapingbee(url, api_key)


NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def parse_listings(html: str) -> List[Dict[str, Any]]:
    m = NEXT_DATA_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    def walk(node):
        if isinstance(node, dict):
            if "items" in node and isinstance(node["items"], list) and node["items"]:
                first = node["items"][0]
                if isinstance(first, dict) and ("id" in first or "slug" in first):
                    return node["items"]
            for v in node.values():
                r = walk(v)
                if r: return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r: return r
        return None
    return [x for x in (walk(data) or []) if isinstance(x, dict)]


def _num(v) -> Optional[float]:
    if v is None: return None
    try: return float(str(v).replace(",", ".").replace(" ", ""))
    except: return None



# ───────────────────────── Wspólne dla wszystkich scraperów (importowane przez olx/nol/portals) ─────────────────────────
_AGENCY_JUNK = re.compile(r"^(osoba prywatna|prywatny|private|właściciel|wlasciciel|brak|none|null|-+)$", re.I)


def clean_agency(name) -> Optional[str]:
    """Nazwa biura/dewelopera z portalu → krótki, czysty string albo None (żeby nie nadpisywać istniejącej wartości pustką)."""
    if not name or not isinstance(name, str): return None
    s = re.sub(r"\s+", " ", name).strip(" .,-–|")[:120]
    if len(s) < 2 or _AGENCY_JUNK.match(s): return None
    return s


def price_history_ops(coll, source: str, docs: List[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    """📈 Historia cen: dla dokumentów, które już są w bazie i mają INNĄ cenę, zwraca {external_id: wpis do $push price_history}.
    Wpis = {price: STARA cena, at: teraz} (aktualna cena zawsze w polu `price`). Jedno zapytanie na batch."""
    ids = [d.get("external_id") for d in docs if d.get("external_id") is not None]
    if not ids: return {}
    out: Dict[Any, Dict[str, Any]] = {}
    try:
        old = {x["external_id"]: x.get("price") for x in coll.find({"source": source, "external_id": {"$in": ids}}, {"external_id": 1, "price": 1})}
    except Exception:
        return {}
    now_iso = datetime.now(timezone.utc).isoformat()
    for d in docs:
        ext = d.get("external_id")
        if ext not in old: continue
        op, np_ = _num(old.get(ext)), _num(d.get("price"))
        if op and np_ and abs(op - np_) >= 1:
            out[ext] = {"price": op, "at": now_iso}
    return out


def with_price_history(update: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Dokleja $push price_history (max 30 wpisów) do gotowego dokumentu UpdateOne."""
    if entry:
        update["$push"] = {"price_history": {"$each": [entry], "$slice": -30}}
    return update


DZIALKA_SUBTYPES = [
    ("budowlana",    ["budowlan"]),
    ("rolna",        ["rolna", "rolne", "rolny"]),
    ("rekreacyjna",  ["rekreacyj"]),
    ("siedliskowa",  ["siedlisk"]),
    ("lesna",        ["leśn", "lesn"]),
    ("inwestycyjna", ["usługow", "uslugow", "komercyj", "inwestycyj"]),
]
DOM_SUBTYPES = [  # wartości ASCII = identyczne z chipami w szukaj.html (data-value) i filtrem API building_type
    ("wolnostojacy", ["wolnostoj", "jednorodzinn"]),
    ("blizniak",     ["bliźn", "blizn", "dwurodzin"]),
    ("szeregowiec",  ["szereg", "segment"]),
    ("siedliskowy",  ["siedlisk", "zagrod"]),
    ("rezydencja",   ["rezydencj", "willa", "dworek"]),
    ("letniskowy",   ["letnisk", "rekreacyjn"]),
    ("kamienica",    ["kamienic"]),
]


def normalize(item: Dict[str, Any], city_name: str, prop: str) -> Optional[Dict[str, Any]]:
    slug = item.get("slug") or item.get("url")
    ext_id = str(item.get("id") or item.get("advertId") or slug or "")
    if not ext_id or not slug: return None
    url = f"https://www.otodom.pl/pl/oferta/{slug}" if not slug.startswith("http") else slug

    title = item.get("title") or PROPERTY_LABELS[prop].capitalize()
    text_blob = f"{title} {item.get('shortDescription','')} {item.get('description','')}".lower()

    # ── Prawdziwa data publikacji z Otodomu ──
    # Otodom podaje: "dateCreated" (YYYY-MM-DD HH:MM:SS) i "createdAtFirst" (ISO).
    # Preferujemy pierwsze wystawienie ("createdAtFirst"), fallback do "dateCreated".
    date_raw = item.get("createdAtFirst") or item.get("dateCreated") or item.get("createdAt") or ""
    posted_iso = None
    if date_raw:
        s = str(date_raw).replace("T", " ").replace("Z", "")[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                posted_iso = dt.isoformat()
                break
            except (ValueError, TypeError):
                pass

    # ── Seller type detection ──
    seller_type = "posrednik"
    if item.get("isPrivateOwner"):
        seller_type = "prywatna"
    elif item.get("isDeveloperOwner"):
        seller_type = "deweloper"
    else:
        adv_type = (item.get("extendedAdvertiserType") or item.get("advertiserType") or "").upper()
        if adv_type == "PRIVATE":
            seller_type = "prywatna"
        elif adv_type == "DEVELOPER":
            seller_type = "deweloper"

    # Subtype detection
    subtype = None
    subtype_field = None
    if prop == "dzialka":
        subtype_source = str(item.get("dzialkaType") or item.get("estateType") or "").lower()
        # Najpierw próba po polu z Otodomu, potem ZAWSZE fallback do tekstu (title+desc)
        for label, kws in DZIALKA_SUBTYPES:
            if any(kw in subtype_source for kw in kws):
                subtype = label; break
        if not subtype:
            for label, kws in DZIALKA_SUBTYPES:
                if any(kw in text_blob for kw in kws):
                    subtype = label; break
        subtype_field = "dzialka_type"
        base = "działka"
    elif prop == "dom":
        subtype_source = str(item.get("buildingType") or item.get("houseType") or "").lower()
        for label, kws in DOM_SUBTYPES:
            if any(kw in subtype_source for kw in kws):
                subtype = label; break
        if not subtype:
            for label, kws in DOM_SUBTYPES:
                if any(kw in text_blob for kw in kws):
                    subtype = label; break
        subtype_field = "building_type"
        base = "dom"
    else:  # mieszkanie
        base = "mieszkanie"

    prop_type = f"{base} {subtype}" if subtype else base

    # price + area
    price = None
    tp = item.get("totalPrice") or {}
    if isinstance(tp, dict): price = _num(tp.get("value"))
    if not price: price = _num(item.get("price"))
    area_m2 = _num(item.get("areaInSquareMeters")) or _num(item.get("area"))

    # rooms (for mieszkania/domy)
    rooms = _num(item.get("roomsNumber"))

    # location
    loc = item.get("location") or {}
    location = ""
    city_part = district = ""
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            city_part = (addr.get("city") or {}).get("name", "") if isinstance(addr.get("city"), dict) else ""
            district = (addr.get("district") or {}).get("name", "") if isinstance(addr.get("district"), dict) else ""
        # tryb --regions: address.city bywa puste → miasto z reverseGeocoding.locations
        # (np. [{"fullName": "Kraków, małopolskie", "locationLevel": "city"}, {"fullName": "Krowodrza, Kraków, małopolskie", "locationLevel": "district"}])
        if not city_part:
            rg = loc.get("reverseGeocoding") or {}
            locs = rg.get("locations") if isinstance(rg, dict) else None
            if isinstance(locs, list):
                for x in locs:
                    if not isinstance(x, dict): continue
                    lvl = str(x.get("locationLevel") or "").lower()
                    parts = [p.strip() for p in str(x.get("fullName") or x.get("name") or "").split(",") if p.strip()]
                    if lvl in ("district", "sub_district", "subdistrict", "neighbourhood", "neighborhood") and len(parts) >= 2:
                        city_part = city_part or parts[1]
                        district = district or parts[0]
                    elif parts and not parts[0].islower() and lvl not in ("region", "province", "voivodeship", "county", "powiat", "country"):
                        city_part = parts[0]   # city/town/village/locality — kolejne wpisy są coraz dokładniejsze
                if not city_part and locs:
                    last = locs[-1] if isinstance(locs[-1], dict) else {}
                    parts = [p.strip() for p in str(last.get("fullName") or "").split(",") if p.strip()]
                    # ostatni element to najdokładniejsza lokalizacja („Chełmiec, nowosądecki, małopolskie” → Chełmiec);
                    # nazwy pisane małą literą to powiat/województwo — nie są miejscowością
                    if parts and not parts[0].islower(): city_part = parts[0]
        location = ", ".join(x for x in [city_part, district] if x)
    if not location: location = city_name
    real_city = city_part if (isinstance(loc, dict) and city_part) else city_name

    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    lat = _num((coords or {}).get("latitude"))
    lon = _num((coords or {}).get("longitude"))

    # ── Obrazek — próba wielu wariantów Otodom API ──
    img_url = None
    images = item.get("images") or item.get("photos") or []
    if isinstance(images, list) and images:
        if isinstance(images[0], dict):
            img_url = (images[0].get("large") or images[0].get("medium")
                       or images[0].get("small") or images[0].get("url"))
        elif isinstance(images[0], str) and images[0].startswith("http"):
            img_url = images[0]
    # Fallback: pole "image", "mainImage", "photoUrl", "thumbnail"
    if not img_url:
        for key in ("image", "mainImage", "photoUrl", "thumbnail", "coverImage", "previewImage"):
            v = item.get(key)
            if isinstance(v, str) and v.startswith("http"):
                img_url = v; break
            if isinstance(v, dict):
                img_url = v.get("large") or v.get("medium") or v.get("url") or v.get("src")
                if img_url: break

    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "source": "otodom",
        "external_id": ext_id,
        "type": prop_type,
        "title": title,
        "url": url,
        "location": location,
        "city": real_city,
        "price": price,
        "area_m2": area_m2,
        "price_pm2": round(price / area_m2, 2) if price and area_m2 and area_m2 > 0 else None,
        "rooms": int(rooms) if rooms else None,
        "lat": lat,
        "lng": lon,
        "image": img_url,
        "transaction_type": TRANSACTION,
        "seller_type": seller_type,
        # added_at = prawdziwa data publikacji na Otodomie (fallback: teraz jeśli nie ma daty)
        "added_at": posted_iso or now_iso,
        "posted_at": posted_iso,          # oryginalna data ISO z Otodomu (jeśli była)
        "last_seen_at": now_iso,          # kiedy ostatnio widzieliśmy ofertę (live)
        "scraped_via": "scrapingbee",   # (ta sama wartość także dla trybu bezpośredniego — filtry API)
    }
    # 🏗️ rynek pierwotny/wtórny od razu przy imporcie (filtr „pierwotny” nie widział nowych ofert bez market_type)
    mt = infer_market(item.get("market"), title, f"{item.get('shortDescription') or ''} {item.get('description') or ''}", None, seller_type, prop_type)
    if mt: doc["market_type"] = mt
    if subtype_field and subtype:
        doc[subtype_field] = subtype
    # 🏢 Nazwa biura / dewelopera (lista wyników Otodom: item.agency.name) — tylko gdy nie-prywatna
    if seller_type != "prywatna":
        ag = item.get("agency") if isinstance(item.get("agency"), dict) else {}
        an = clean_agency(ag.get("name"))
        if an: doc["agency_name"] = an
    # Wielkość działki (domy, działki) — Otodom podaje ją już na liście wyników
    try:
        ta = item.get("terrainAreaInSquareMeters")
        if ta and float(ta) > 0 and prop in ("dom", "dzialka"):
            doc["plot_area_m2"] = float(ta)
    except (TypeError, ValueError):
        pass
    return doc


# ── Dociąganie ze strony oferty: telefon właściciela (PRYWATNE) + wielkość działki (DOMY) ────────
# __NEXT_DATA__ strony oferty: ad.contactDetails.phones (prywatni jawnie) i ad.target.Terrain_area (lista wyników tego nie ma).
def _fetch_detail_ad(url: str) -> Optional[Dict[str, Any]]:
    html = fetch_direct(url)
    if not html:
        return None
    m = NEXT_DATA_RE.search(html)
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["ad"] if m else {}
    except Exception:
        return {}


def enrich_from_details(coll, prop_type: str, transaction: str, phone_limit: int, plot_limit: int) -> Dict[str, int]:
    """Dociąga ze stron ofert: (a) telefon właściciela dla ofert PRYWATNYCH, (b) wielkość działki dla DOMÓW (każdy sprzedający).
    Każdą ofertę odwiedzamy raz (detail_checked_at). Limity na uruchomienie chronią przed blokadą."""
    stats = {"checked": 0, "phones": 0, "plots": 0, "failed": 0}
    base = {"source": "otodom", "transaction_type": transaction, "is_stale": {"$ne": True},
            "type": {"$regex": f"^{re.escape(prop_type)}", "$options": "i"}, "detail_checked_at": {"$exists": False}}
    targets: List[Dict[str, Any]] = []
    if phone_limit > 0:
        targets += list(coll.find({**base, "seller_type": "prywatna", "phone": {"$exists": False}}, {"url": 1}).sort("added_at", -1).limit(phone_limit))
    if plot_limit > 0 and prop_type == "dom":
        seen = {t["_id"] for t in targets}
        targets += [t for t in coll.find({**base, "plot_area_m2": {"$exists": False}}, {"url": 1}).sort("added_at", -1).limit(plot_limit) if t["_id"] not in seen]
    now_iso = datetime.now(timezone.utc).isoformat()
    streak = 0
    for d in targets:
        stats["checked"] += 1
        ad = _fetch_detail_ad(d["url"])
        if ad is None:
            stats["failed"] += 1; streak += 1
            if streak >= 8:
                log.warning("  detale: 8 nieudanych pobrań z rzędu — przerywam (blokada?)"); break
            time.sleep(2.0); continue
        streak = 0
        upd: Dict[str, Any] = {"detail_checked_at": now_iso}
        cd = ad.get("contactDetails") or {}
        phones = [p for p in (cd.get("phones") or []) if p]
        if (cd.get("type") or (ad.get("owner") or {}).get("type")) == "private" and phones:
            upd["phone"] = re.sub(r"[^\d+]", "", str(phones[0])); stats["phones"] += 1
        ta = (ad.get("target") or {}).get("Terrain_area")
        if ta:
            try:
                upd["plot_area_m2"] = float(str(ta).replace(",", ".")); stats["plots"] += 1
            except ValueError:
                pass
        coll.update_one({"_id": d["_id"]}, {"$set": upd})
        time.sleep(1.0)
    log.info("  detale (%s/%s): sprawdzono=%d telefonów=%d działek=%d nieudane=%d",
             prop_type, transaction, stats["checked"], stats["phones"], stats["plots"], stats["failed"])
    return stats


def get_mongo():
    url = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    if not url: log.error("MONGODB_URI not set"); sys.exit(1)
    client = MongoClient(url, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    return client[os.environ.get("MONGODB_DB") or "finderdom"]


_JOB_STARTED_AT = time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--property", choices=list(PROPERTY_SLUGS.keys()), required=True)
    ap.add_argument("--city", help="One city slug (lowercase)")
    ap.add_argument("--max-pages", type=int, default=15)
    ap.add_argument("--transaction", choices=["sprzedaz", "wynajem"], default="sprzedaz",
                    help="sprzedaz (domyślnie) lub wynajem — wynajem trafia do bazy z transaction_type=wynajem")
    ap.add_argument("--regions", action="store_true", help="16 województw zamiast 20 miast (cała Polska, od najnowszych)")
    args = ap.parse_args()
    global TRANSACTION
    TRANSACTION = args.transaction

    api_key = os.environ.get("SCRAPINGBEE_API_KEY") or None
    if not api_key: log.warning("SCRAPINGBEE_API_KEY not set — tylko tryb bezpośredni (bez fallbacku)")

    db = get_mongo()
    coll = db["listings"]

    # ── One-time migration: convert datetime added_at → ISO string ──
    # (needed because frontend filters expect string date)
    try:
        migrated = coll.update_many(
            {"scraped_via": "scrapingbee", "added_at": {"$type": "date"}},
            [{"$set": {"added_at": {"$dateToString": {
                "format": "%Y-%m-%dT%H:%M:%S.%LZ", "date": "$added_at"}}}}]
        ).modified_count
        if migrated:
            log.info("Migrated %d docs: added_at datetime → ISO string", migrated)
    except Exception as e:
        log.warning("added_at migration skipped: %s", e)

    cities = REGIONS if args.regions else CITIES
    if args.city:
        cities = [c for c in CITIES if c["slug"].split("/")[0] == args.city.lower()]
        if not cities: log.error("Unknown city: %s", args.city); sys.exit(1)

    log.info("═══ Otodom scraper — transaction=%s property=%s cities=%d max_pages=%d ═══",
             TRANSACTION, args.property, len(cities), args.max_pages)
    total = {"new": 0, "updated": 0, "credits": 0}

    for city in cities:
        log.info("── %s ──", city["name"])
        empty_streak = 0
        for page in range(1, args.max_pages + 1):
            url = otodom_url(args.property, city, page)
            html = fetch_page(url, api_key)
            if not html:
                log.warning("  page %d: fetch failed", page); break
            items = parse_listings(html)
            if not items:
                log.info("  page %d: no listings", page); break

            docs = [d for d in (normalize(it, city["name"], args.property) for it in items) if d]
            if docs and city["slug"] == city["voi"]:   # diagnostyka trybu regionów: ile ofert ma rozpoznane miasto
                no_city = sum(1 for d in docs if d.get("city") == city["name"])
                log.info("  miasto rozpoznane: %d/%d", len(docs) - no_city, len(docs))
                if no_city and not DIRECT_STATE.get("loc_dumped"):
                    DIRECT_STATE["loc_dumped"] = True
                    bad = next((it for it in items if (it.get("location") or {}) and normalize(it, city["name"], args.property) and normalize(it, city["name"], args.property).get("city") == city["name"]), None)
                    if bad: log.info("  przykład location bez miasta: %s", json.dumps(bad.get("location"), ensure_ascii=False)[:700])
            batch_new = batch_upd = 0
            if docs:
                # added_at = zawsze prawdziwa data z Otodomu (posted_at) — pozwala poprawnie działać
                # filtrom "dziś / 3 dni / 7 dni" nawet po wielokrotnych scrape'ach.
                ph = price_history_ops(coll, docs[0]["source"], docs)
                if ph or any(d.get("agency_name") for d in docs):
                    log.info("  📈 zmiany cen: %d · 🏢 z nazwą biura: %d/%d", len(ph), sum(1 for d in docs if d.get("agency_name")), len(docs))
                ops = [UpdateOne({"source": d["source"], "external_id": d["external_id"]},
                                 with_price_history({"$set": d}, ph.get(d["external_id"])), upsert=True) for d in docs]
                try:
                    r = coll.bulk_write(ops, ordered=False)
                    batch_new = r.upserted_count
                    batch_upd = r.modified_count
                    total["new"] += batch_new
                    total["updated"] += batch_upd
                except Exception as e:
                    log.warning("  bulk_write: %s", e)
            log.info("  page %d: %d items → new=%d, updated=%d (total_new=%d)",
                     page, len(items), batch_new, batch_upd, total["new"])
            if batch_new == 0 and batch_upd == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    log.info("  → 2 empty pages, next city"); break
            else:
                empty_streak = 0
            time.sleep(1.0)

    log.info("═══ DONE property=%s: new=%d updated=%d | direct_ok=%d (%s) sb_credits=%d ═══",
             args.property, total["new"], total["updated"], DIRECT_STATE["direct_ok"], _DIRECT_KIND, DIRECT_STATE["sb_used"])

    # Telefony właścicieli (oferty prywatne) — po imporcie listy, bez ScrapingBee (tylko bezpośrednio)
    try:
        fresh = args.max_pages <= 2
        enrich_from_details(coll, PROPERTY_LABELS.get(args.property, args.property), TRANSACTION,
                            int(os.environ.get("OTODOM_PHONE_LIMIT") or ("40" if fresh else "120")),
                            int(os.environ.get("OTODOM_PLOT_LIMIT") or ("60" if fresh else "250")))
    except Exception as e:
        log.warning("detail enrichment skipped: %s", e)

    # ⏰ Godzinne odświeżanie 9–19 PL: job-lider (mieszkanie/sprzedaz, tryb regiony) odpala kolejną lekką turę importów
    if args.regions and args.property == "mieszkanie" and TRANSACTION == "sprzedaz":
        try:
            import fresh_sweep
            fresh_sweep.wait_and_dispatch_next(db, _JOB_STARTED_AT)
        except Exception as e:
            log.warning("fresh sweep loop skipped: %s", e)


# ── Lokale użytkowe: matryce w .github/workflows nie mają „lokal” (nie da się ich edytować bez uprawnienia „Workflows”
#    na tokenie GitHub), więc lokal (sprzedaż + wynajem) jedzie w tym samym zadaniu co działki. 2.11.2026
CHAIN_AFTER = {"dzialka": [("lokal", "sprzedaz"), ("lokal", "wynajem")]}


def _run_with_chain():
    main()
    argv = sys.argv[1:]
    if "--property" not in argv:
        return
    prop = argv[argv.index("--property") + 1]
    for p2, t2 in CHAIN_AFTER.get(prop, []):
        nxt = list(argv)
        nxt[nxt.index("--property") + 1] = p2
        if "--transaction" in nxt:
            nxt[nxt.index("--transaction") + 1] = t2
        else:
            nxt += ["--transaction", t2]
        sys.argv = [sys.argv[0]] + nxt
        log.info("═══ CHAIN → property=%s transaction=%s ═══", p2, t2)
        try:
            main()
        except SystemExit:
            pass
        except Exception as e:  # lokal nie może wywrócić całego zadania
            log.error("CHAIN %s/%s failed: %s", p2, t2, str(e)[:200])


if __name__ == "__main__":
    _run_with_chain()
