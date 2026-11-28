#!/usr/bin/env python3
"""
Darmowy scraper (bez proxy, 0 zł) dla portali: morizon, gratka, domiporta, adresowo → MongoDB `listings`.
Strony list są renderowane serwerowo; pobieramy tylko listy (bez szczegółów), więc 1 zapytanie = 35–40 ofert.

Użycie:
  python scripts/scrape_portals.py --portal morizon --property mieszkanie --transaction sprzedaz [--city krakow] [--max-pages 5]
Env: MONGODB_URI (lub MONGO_URL), MONGODB_DB
"""
import argparse, html as htmllib, json, logging, os, re, sys, time, unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_otodom import CITIES, REGIONS, DOM_SUBTYPES, DZIALKA_SUBTYPES, _num, city_from_slug, clean_agency, price_history_ops, with_price_history  # noqa: E402
from market_type import infer_market, infer_rooms, infer_build_year  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("portals")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
S = requests.Session()
_JOB_STARTED_AT = time.time()
S.headers.update({"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,*/*;q=0.8"})
BASE_LABEL = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "działka", "lokal": "lokal"}


def city_slug(city: Dict[str, str]) -> str:
    return city["slug"].split("/")[0]


def is_region(city: Dict[str, str]) -> bool:
    return city.get("slug") == city.get("voi")


# ───────────────────────── URL builders ─────────────────────────
def url_morizon(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "komercyjne"}[prop]   # Morizon: lokale = sekcja „komercyjne”
    newest = "najnowsze/" if is_region(city) else ""
    base = f"https://www.morizon.pl/{'do-wynajecia/' if trans == 'wynajem' else ''}{seg}/{newest}{city_slug(city)}/"
    return base + (f"?page={page}" if page > 1 else "")


def url_gratka(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki-grunty", "lokal": "lokale-uzytkowe"}[prop]
    base = f"https://gratka.pl/nieruchomosci/{seg}/{city_slug(city)}" + ("/wynajem" if trans == "wynajem" else "")
    q = ("?sort=newest" if is_region(city) else "") + (f"{'&' if is_region(city) else '?'}page={page}" if page > 1 else "")
    return base + q


def url_domiporta(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "dzialke", "lokal": "lokal-uzytkowy"}[prop]
    t = "wynajme" if trans == "wynajem" else "sprzedam"
    base = f"https://www.domiporta.pl/{seg}/{t}/{city['voi']}" + ("" if is_region(city) else f"/{city_slug(city)}")
    return base + (f"?PageNumber={page}" if page > 1 else "")


def url_adresowo(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "lokale"}[prop]
    if trans == "wynajem": seg += "-wynajem"
    base = f"https://adresowo.pl/{seg}/" + ("" if city_slug(city) == "polska" else f"{city_slug(city)}/")
    return base + (f"_l{page}" if page > 1 else "")


def url_gethome(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki"}.get(prop)
    if not seg:
        return None   # gethome: lokale za logowaniem (302 → kontomieszkaniowe)
    t = "do-wynajecia" if trans == "wynajem" else "na-sprzedaz"
    loc = city_slug(city) if city_slug(city) != "polska" else ""
    return f"https://gethome.pl/{seg}/{t}/" + (f"{loc}/" if loc else "") + f"?sort=newest&page={page}"


def url_odwlasciciela(prop, trans, city, page):
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki"}.get(prop)
    if not seg:
        return None   # portal nie ma kategorii lokali
    loc = city.get("voi") or city_slug(city)
    if city.get("voi") and city_slug(city) not in (city.get("voi"), "polska"):
        loc = f"{city['voi']},{city_slug(city)}"
    return f"https://odwlasciciela.pl/{seg}/{'wynajem' if trans == 'wynajem' else 'sprzedaz'}/{loc}.html" + (f"?offset={(page - 1) * 20}" if page > 1 else "")


def url_listaprzetargow(prop, trans, city, page):
    """listaprzetargow.pl — przetargi i licytacje komornicze (tylko sprzedaż). Lista ogólnopolska od najnowszych, 25/str."""
    if trans != "sprzedaz":
        return None
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "lokale-uzytkowe"}[prop]
    loc = "" if city_slug(city) in ("polska", city.get("voi")) else f"/{city_slug(city)}"
    return f"https://listaprzetargow.pl/oferty/{seg}{loc}?sort=created_at-desc&page={page}"


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
        # od 11.2026 Morizon/Gratka pakują dane w {"@graph": [Product, BreadcrumbList, ...]}
        nodes = d.get("@graph") if isinstance(d, dict) and isinstance(d.get("@graph"), list) else (d if isinstance(d, list) else [d])
        d = next((n for n in nodes if isinstance(n, dict) and n.get("@type") == "Product"), None)
        if not d: continue
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
                "agency": _name_of(o.get("seller") or o.get("offeredBy") or it.get("seller") or d.get("brand")),
            })
        break
    return out


def _name_of(v) -> Optional[str]:
    """Nazwa z węzła ld+json/JSON (dict z name/legalName) albo string."""
    if isinstance(v, dict): return v.get("name") or v.get("legalName") or None
    return v if isinstance(v, str) else None


def parse_gethome(html: str) -> List[Dict[str, Any]]:
    """gethome.pl: pełna lista w window.__INITIAL_STATE__ → offerList.offers.offers (JSON)."""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html or "")
    if not m: return []
    try:
        state, _ = json.JSONDecoder().raw_decode(html[m.end():])
        offers = (((state.get("offerList") or {}).get("offers") or {}).get("offers")) or []
    except Exception:
        return []
    out = []
    for o in offers:
        slug = o.get("slug") or ""
        mid = re.search(r"-(\d+)$", slug)
        if not slug or not mid: continue
        pr, prop, addr = o.get("price") or {}, o.get("property") or {}, (o.get("property") or {}).get("address_details") or {}
        pics = o.get("pictures") or []
        img = next((v for v in (pics[0] or {}).values() if isinstance(v, str) and v.startswith("http")), None) if pics else None
        coords = o.get("coordinates") or {}
        loc_path = (prop.get("location") or {}).get("path") or []
        city_name = addr.get("city") or next((x.get("name") for x in reversed(loc_path) if isinstance(x, dict) and (x.get("type") or "").lower().startswith(("miasto", "gmina", "miejscowo"))), None)
        out.append({
            "id": mid.group(1), "url": f"https://gethome.pl/oferta/{slug}/", "title": o.get("name") or "",
            "price": _num(pr.get("total")), "area": _num(prop.get("size")), "rooms": _num(prop.get("room_number")),
            "floor": _num(prop.get("floor")), "image": img, "district": None, "street": addr.get("street"),
            "loc0": city_name, "desc": (o.get("description") or "")[:2000],
            "seller": "prywatna" if o.get("is_private") else ("deweloper" if o.get("market_type") == "primary_market" else "posrednik"),
            "market": o.get("market_type"),
            "lat": coords.get("lat"), "lng": coords.get("lon"), "plot_area_m2": _num(prop.get("lot_size")),
            "posted_at": o.get("created_at"), "build_year": _num(prop.get("building_year")),
            "agency": _name_of(o.get("agency") or o.get("agent") or o.get("company") or o.get("developer") or ((o.get("investment") or {}).get("developer") if isinstance(o.get("investment"), dict) else None)),
        })
    return out


def parse_odwlasciciela(html: str) -> List[Dict[str, Any]]:
    """odwlasciciela.pl: tylko oferty bezpośrednio od właścicieli (karty <article class="offers-item">)."""
    out = []
    for a in re.findall(r'<article class="offers-item[^"]*">(.*?)</article>', html or "", re.S):
        href = re.search(r'href="(/oferty/podglad/(\d+),[^"]+)"', a)
        if not href: continue
        city = re.search(r'<strong class="text--gray-2 text--medium">(.*?)</strong>', a, re.S)
        street = re.search(r'<div class="text--gray-3">(.*?)</div>', a, re.S)
        kind = re.search(r'<span class="offers-item__attribute"><strong>([^<]*(?:sprzeda|wynaj|dzier)[^<]*)</strong>', a, re.I)
        area = re.search(r'<strong>([\d\s.,]+)</strong>\s*m2', a)
        rooms = re.search(r'L\.pokoi:\s*<strong>(\d+)</strong>', a)
        floor = re.search(r'Piętro:\s*<strong>(\d+)', a)
        price = re.search(r'font-weight-bold">([\d\s&nbsp;]+)<small>PLN</small>', a)
        img = re.search(r'<img src="(/assets/userfiles/[^"]+)"', a)
        desc = re.search(r'text--small text--gray-2[^"]*">(.*?)</div>', a, re.S)
        c = _txt(city.group(1)) if city else None
        out.append({
            "id": href.group(2), "url": "https://odwlasciciela.pl" + href.group(1),
            "title": " ".join(x for x in [_txt(kind.group(1)) if kind else "", c or "", _txt(street.group(1)) if street else ""] if x),
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": _num(rooms.group(1)) if rooms else None, "floor": _num(floor.group(1)) if floor else None,
            "image": ("https://odwlasciciela.pl" + img.group(1)) if img else None, "district": None,
            "street": _txt(street.group(1)) if street else None, "loc0": c, "desc": _txt(desc.group(1)) if desc else "",
            "seller": "prywatna",
        })
    return out


def enrich_odwlasciciela_phone(url: str) -> Optional[str]:
    """Strona oferty odwlasciciela.pl ma numer ogłoszeniodawcy w ld+json: "provider": {"@type": "Person", "telephone": "505258412"}."""
    html = fetch(url)
    if not html: return None
    m = re.search(r'"provider"\s*:\s*\{[^}]*"telephone"\s*:\s*"([^"]+)"', html)
    if not m: return None
    digits = re.sub(r"\D", "", m.group(1))
    if len(digits) == 9: return "+48" + digits
    if len(digits) == 11 and digits.startswith("48"): return "+" + digits
    return None


def parse_morizon(html): return parse_ldjson_offers(html, r"-mzn(\d+)$")
def parse_gratka(html): return parse_ldjson_offers(html, r"/ob/(\d+)")


def parse_domiporta(html: str) -> List[Dict[str, Any]]:
    out = []
    for a in re.findall(r'<article class="sneakpeak[^"]*"(.*?)</article>', html, re.S):
        # 16.11: Kraków strona 1 = same inwestycje deweloperskie: data-detail-id="/DPRP/658224", href="/nowe/ogloszenie/…_658224"
        mid = re.search(r'data-detail-id="(\d+|/DPRP/\d+)"', a)
        href = re.search(r'href="(/nieruchomosci/[^"]+/\d+|/nowe/ogloszenie/[^"]+)"', a)
        if not mid or not href: continue
        dprp = mid.group(1).startswith("/DPRP/")
        title = re.search(r'<h2 class="sneakpeak__title--bold">(.*?)</h2>', a, re.S)
        loc = re.search(r'<span class="sneakpeak__title--inblock">(.*?)</span>', a, re.S)
        price = re.search(r'<span class="sneakpeak__price_value">(.*?)</span>', a, re.S)
        area = re.search(r'sneakpeak__details_item--area"[^>]*>(.*?)</span>', a, re.S)
        rooms = re.search(r'sneakpeak__details_item--room"[^>]*>(.*?)</span>', a, re.S)
        img = re.search(r'<img[^>]+src="(https://galeria\.domiporta\.pl/[^"]+)"', a)
        desc = re.search(r'<p class="sneakpeak__[a-z_]*"[^>]*>([^<]{5,})', a)
        loc_parts = [p.strip() for p in _txt(loc.group(1)).split(",")] if loc else []
        out.append({
            "id": ("dprp-" + mid.group(1).rsplit("/", 1)[1]) if dprp else mid.group(1), "url": "https://www.domiporta.pl" + href.group(1), "title": _txt(title.group(1)) if title else "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": _num(_txt(rooms.group(1))) if rooms else None, "floor": None, "image": img.group(1) if img else None,
            # „Kraków, Bronowice, Zgodna 34” → dzielnica + ulica; „Kraków, Zgodna 34” (numer) → tylko ulica
            "district": loc_parts[1] if len(loc_parts) > 2 or (len(loc_parts) == 2 and not re.search(r"\d", loc_parts[1])) else None,
            "street": loc_parts[-1] if (len(loc_parts) > 2 or (len(loc_parts) == 2 and re.search(r"\d", loc_parts[1]))) else None,
            "loc0": loc_parts[0] if loc_parts else None,
            "desc": _txt(desc.group(1)) if desc else "", "seller": "deweloper" if dprp else None,
            **({"market": "pierwotny"} if dprp else {}),
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
            "image": img.group(1) if img else None, "district": district, "street": street, "loc0": loc,
            # adresowo: karta prywatna ma etykietę „bez pośredników”; pozostałe (~40%) to agencje (strona oferty: „Właściciel powierzył sprzedaż agencji”)
            "desc": _txt(desc.group(1)) if desc else "", "seller": "prywatna" if "bez pośredników" in c else "posrednik",
        })
    return out


def verify_adresowo_seller(url: str) -> Optional[str]:
    """Strona oferty adresowo.pl: `re.offerPhoneType = 'realtor'` → agencja; 'owner'/inne → właściciel. None gdy brak HTML."""
    html = fetch(url)
    if not html: return None
    m = re.search(r"PhoneType\s*=\s*'(\w+)'", html)
    if not m: return "prywatna" if "bez pośredników" in html else None
    return "posrednik" if m.group(1) == "realtor" else "prywatna"


PORTALS = {
    "morizon":   {"url": url_morizon,   "parse": parse_morizon},
    "gratka":    {"url": url_gratka,    "parse": parse_gratka},
    "domiporta": {"url": url_domiporta, "parse": parse_domiporta},
    "adresowo":  {"url": url_adresowo,  "parse": parse_adresowo},
    # nowe portale 5.11.2026 (odpalane łańcuchem po istniejących — patrz PORTAL_CHAIN)
    "gethome":        {"url": url_gethome,        "parse": parse_gethome},
    "odwlasciciela":  {"url": url_odwlasciciela,  "parse": parse_odwlasciciela},
    "listaprzetargow": {"url": url_listaprzetargow, "parse": None},   # parse ustawiony niżej (funkcja zdefiniowana dalej)
}


def parse_listaprzetargow(html: str) -> List[Dict[str, Any]]:
    """Karty <a href=".../oferty/{id}-{slug}" class="modernCard ...">: lokalizacja „Radom, Południe”, typ, cena (wywoławcza),
    zł/m², metraż, tag źródła (Przetarg / Licytacja komornicza / Syndyk), termin (DD-MM-RRRR), zdjęcie, badge obniżki (-40%)."""
    out = []
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    for m in re.finditer(r'<a href="(https://listaprzetargow\.pl/oferty/(\d+)-[^"]+)"[^>]*class="modernCard[^"]*"[^>]*>(.*?)</a>\s*(?=<a |<div|<nav|$)', html, re.S):
        url, mid, c = m.group(1), m.group(2), m.group(3)
        loc = re.search(r'modernCard__location[^>]*>(.*?)</p>', c, re.S)
        ftype = re.search(r'modernCard__facilityType[^>]*>(.*?)</div>', c, re.S)
        price = re.search(r'modernCard__priceMain[^>]*>(.*?)</div>', c, re.S)
        area = re.search(r'modernCard__tag--area.*?<span>\s*(.*?)\s*</span>', c, re.S)
        src = re.search(r'modernCard__tag--source[^>]*>(.*?)</span>', c, re.S)
        deadline = re.search(r'modernCard__deadline[^>]*>.*?(\d{2}-\d{2}-\d{4})', c, re.S)
        img = re.search(r'<img src="(https://[^"]+)"', c)
        red = re.search(r'reductionBadge[^>]*>\s*(-?\d+)\s*%', c, re.S)
        loc_txt = _txt(loc.group(1)) if loc else ""
        parts = [p.strip() for p in loc_txt.split(",") if p.strip()]
        area_v = None
        if area:
            a_txt = _txt(area.group(1)); area_v = _money(a_txt)
            if area_v and re.search(r"\bha\b", a_txt): area_v *= 10000      # działki: „1.2895 ha”
            elif area_v and re.search(r"\bar(?:y|ów)?\b", a_txt): area_v *= 100
        kind = _txt(src.group(1)).lower() if src else "przetarg"
        kind = "licytacja" if "licytac" in kind else "syndyk" if "syndyk" in kind else "przetarg"
        dl = None
        if deadline:
            d_, mo, y = deadline.group(1).split("-"); dl = f"{y}-{mo}-{d_}"
        img_u = img.group(1) if img and "default" not in img.group(1) else None   # _medium → 403, zostaje _small (karta i tak ≤640px)
        out.append({
            "id": mid, "url": url, "title": f"{'Licytacja komornicza' if kind == 'licytacja' else 'Syndyk' if kind == 'syndyk' else 'Przetarg'}: {_txt(ftype.group(1)) if ftype else ''} {loc_txt}".strip(),
            "price": _money(price.group(1)) if price else None, "area": area_v, "rooms": None, "floor": None,
            "image": img_u, "district": parts[1] if len(parts) > 1 and not parts[1].islower() else None, "street": None,   # „Brzeszcze, małopolskie” → to województwo, nie dzielnica
            "loc0": parts[0] if parts else None, "desc": "", "seller": "przetarg",
            "auction_kind": kind, "auction_deadline": dl, "auction_discount_pct": int(red.group(1)) if red else None,
        })
    return out


PORTALS["listaprzetargow"]["parse"] = parse_listaprzetargow


# ───────────────────────── Nowe portale 16.11.2026: okolica.pl, krn.pl, allegrolokalnie.pl ─────────────────────────
# Wcześniejsze próby (requests, zwykły UA) kończyły się 403/Cloudflare. Z odciskiem Chrome (curl_cffi, jak OLX) listy odpowiadają 200.
try:
    from curl_cffi import requests as cffi_requests   # w scripts/requirements.txt (scraper OLX)
except ImportError:   # pragma: no cover
    cffi_requests = None
CFFI_PORTALS = {"okolica", "krn", "allegrolokalnie"}
# tryb --regions: te portale mają listę ogólnopolską „od najnowszych” (jak adresowo) zamiast 16 województw
NATIONAL_LIST_PORTALS = {"adresowo", "listaprzetargow", "okolica", "krn", "allegrolokalnie"}


def _rel_date(txt: Optional[str]) -> Optional[str]:
    """„19 godz temu” / „3 dni temu” / „1 min temu” / „wczoraj” → ISO (UTC). Dokładność listy okolica.pl: godziny/dni."""
    if not txt: return None
    t = txt.lower().strip()
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    m = re.search(r"(\d+)\s*(min|godz|dni|dzie|tyg|mies)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"min": timedelta(minutes=n), "godz": timedelta(hours=n), "dni": timedelta(days=n), "dzie": timedelta(days=n),
                 "tyg": timedelta(weeks=n), "mies": timedelta(days=30 * n)}[unit]
        return (now - delta).isoformat()
    if "wczoraj" in t: return (now - timedelta(days=1)).isoformat()
    if "dzisiaj" in t or "dziś" in t or "teraz" in t: return now.isoformat()
    return None


def url_okolica(prop, trans, city, page):
    """okolica.pl — darmowe ogłoszenia, dużo bezpośrednich. Listy SSR: <section class="property">, 25–40/str., sort „data dodania”."""
    seg = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "dzialka", "lokal": "komercyjne/lokal-uzytkowy"}[prop]
    if prop == "dzialka" and trans == "wynajem": return None   # brak kategorii (404)
    t = "wynajme" if trans == "wynajem" else "sprzedam"
    loc = "" if city_slug(city) in ("polska", city.get("voi")) else f"{city_slug(city)}/"
    return f"https://www.okolica.pl/{seg}/{t}/{loc}?browser%5Bsort%5D=add_desc" + (f"&page={page}" if page > 1 else "")


def parse_okolica(html: str) -> List[Dict[str, Any]]:
    out = []
    for m in re.finditer(r'<section class="property"[^>]*data-id="([^"]+)"[^>]*>(.*?)</section>', html, re.S):
        oid, c = m.group(1), m.group(2)
        url = re.search(r'<h2 class="property-title"><a href="([^"]+)"', c)
        title = re.search(r'<h2 class="property-title"><a[^>]*>(.*?)</a>', c, re.S)
        addr = re.search(r'class="property-address"[^>]*>(.*?)</a>', c, re.S)
        price = re.search(r'<li><span class="price">([^<]*)</span>\s*zł</li>', c)
        rooms = re.search(r'Pokoje:\s*(\d+)', c)
        area = re.search(r'<li><span>([\d\s,.]+)</span>\s*m<sup>2</sup></li>', c)
        img = re.search(r'<img class="property-photo-master"[^>]*src="([^"]+)"', c, re.S)
        added = re.search(r'class="added_1">([^<]*)<', c)
        desc = re.search(r'description-short-content">(.*?)</span>', c, re.S)
        agency = re.search(r'<a href="/firma/([^"]+)"', c)
        # „Szczecin, Dąbie, Emilli Gierczak” / „Szczecin, Hieronima Derdowskiego” / „Niemstów (powiat lubiński, gmina Lubin)”
        parts = [p.strip() for p in re.sub(r"\(.*?\)", "", _txt(addr.group(1))).split(",") if p.strip()] if addr else []
        if not url: continue
        out.append({
            "id": oid, "url": url.group(1), "title": _txt(title.group(1)) if title else "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": int(rooms.group(1)) if rooms else None, "floor": None,
            "image": img.group(1) if img else None,
            "district": parts[1] if len(parts) > 2 else None, "street": parts[-1] if len(parts) > 1 else None,
            "loc0": parts[0] if parts else None, "desc": _txt(desc.group(1)) if desc else "",
            # karta z awatarem /firma/… = biuro; bez awatara = ogłoszenie bezpośrednie (sekcja „bezposrednio” portalu nie ma żadnego awatara)
            "seller": "posrednik" if agency else "prywatna",
            "agency": agency.group(1).rsplit("-", 1)[0].replace("-", " ").title() if agency else None,
            "posted_at": _rel_date(added.group(1) if added else None),
        })
    return out


def url_krn(prop, trans, city, page):
    """krn.pl (Krakowski Rynek Nieruchomości) — głównie Kraków/Małopolska, 24/str., sort=1_2 = data od najnowszej."""
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "lokale-uzytkowe"}[prop]
    loc = "" if city_slug(city) in ("polska", city.get("voi")) else f"/{city_slug(city)}"
    return f"https://www.krn.pl/{seg}/{trans}{loc}/s{page}?&sort=1_2"


def parse_krn(html: str) -> List[Dict[str, Any]]:
    out = []
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    html = html.replace("<sup>2</sup>", "²")
    for m in re.finditer(r'<a name="(\d+)" href="(https://www\.krn\.pl/oferta/[^"]+)"[^>]*>(.*?)</a>\s*<span style="display:none;">(.*?)</span>', html, re.S):
        oid, url, c, meta = m.groups()
        title = re.search(r'<h2 class="title">(.*?)</h2>', c, re.S)
        addr = re.search(r'<span class="address">(.*?)</span>', c, re.S)
        rooms_blk = re.search(r'<span class="rooms">(.*?)</span>\s*<span class="added">', c, re.S)
        rb = _txt(rooms_blk.group(1)) if rooms_blk else ""
        rooms = re.search(r'(\d+)\s*pok', rb); floor = re.search(r'piętro:\s*(\d+)', rb)
        area = re.search(r'([\d.,]+)\s*m²\s*$', rb)                       # „5 pokoi · piętro: 2 · 170.90 m²”
        plot = re.search(r'działka:\s*([\d\s.,]+)\s*m', rb)
        # miasto: addressLocality (strona miasta) albo z tytułu „Mieszkanie Jastrzębie-Zdrój 34.50m²” (lista ogólnopolska)
        locality = re.search(r'itemprop="addressLocality">(.*?)<', meta, re.S)
        t_city = re.match(r'^(?:Now[aey]\s+)?(?:Mieszkanie|Dom|Działka|Lokal\S*|Biuro|Kamienica|Garaż)\s+(.+?)\s+[\d.,]+\s*m²', _txt(title.group(1)) if title else "")
        price = re.search(r'<span class="price">([^<]*)</span>', c)
        img = re.search(r'<img src="([^"]+)" class="main-image"', c)
        firm = re.search(r'<span class="firm">(.*?)</span>', c, re.S)
        street = _txt(addr.group(1)) if addr else ""
        loc0 = (_txt(locality.group(1)) if locality else "") or (t_city.group(1).strip() if t_city else "")
        out.append({
            "id": oid, "url": url, "title": _txt(title.group(1)) if title else "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": int(rooms.group(1)) if rooms else None, "floor": int(floor.group(1)) if floor else None,
            "image": htmllib.unescape(img.group(1)) if img else None, "district": None, "street": street or None,
            "loc0": loc0 or None, "desc": "", "seller": None,
            "agency": _txt(firm.group(1)) if firm and _txt(firm.group(1)) else None,
            "plot_area_m2": _money(plot.group(1)) if plot else None,
        })
    return out


AL_CAT = {("mieszkanie", "sprzedaz"): "mieszkania-na-sprzedaz-112739", ("dom", "sprzedaz"): "domy-na-sprzedaz-112740",
          ("dzialka", "sprzedaz"): "dzialki-na-sprzedaz-112741", ("lokal", "sprzedaz"): "lokale-i-obiekty-uzytkowe-na-sprzedaz-112742",
          ("mieszkanie", "wynajem"): "mieszkania-do-wynajecia-112745", ("dom", "wynajem"): "domy-do-wynajecia-112746",
          ("dzialka", "wynajem"): "dzialki-do-wynajecia-112748", ("lokal", "wynajem"): "lokale-i-obiekty-uzytkowe-do-wynajecia-112749"}


def url_allegrolokalnie(prop, trans, city, page):
    """allegrolokalnie.pl — 60 kart/str., sort od najnowszych. ~90% kart to syndykacja z Gratka.pl (badge źródła) — te pomijamy
    (Gratkę importujemy wprost); zostają własne ogłoszenia Allegro (głównie osoby prywatne)."""
    loc = "" if city_slug(city) in ("polska", city.get("voi")) else f"/{city_slug(city)}"
    return f"https://allegrolokalnie.pl/oferty/nieruchomosci/{AL_CAT[(prop, trans)]}{loc}?sort=startingTime%3Adesc" + (f"&page={page}" if page > 1 else "")


def parse_allegrolokalnie(html: str) -> List[Dict[str, Any]]:
    out, skipped = [], 0
    ld: Dict[str, Dict[str, Any]] = {}
    for blk in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try: d = json.loads(blk)
        except Exception: continue
        if isinstance(d, dict) and d.get("@type") == "ItemList":
            for el in d.get("itemListElement") or []:
                it = el.get("item") or {}
                if it.get("url"): ld[it["url"].split("?")[0]] = it
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    for m in re.finditer(r'<a ([^>]*class="mlc-card mlc-itembox"[^>]*)>(.*?)</a>', html, re.S):
        attrs, c = m.group(1), m.group(2)
        href = re.search(r'href="([^"]+)"', attrs)
        if not href: continue
        path = htmllib.unescape(href.group(1)).split("?")[0]
        url = "https://allegrolokalnie.pl" + path if path.startswith("/") else path
        if re.search(r'mlc-itembox__advert-source">\s*<span aria-label="', c):   # syndykacja (Gratka.pl itp.) → pomijamy
            skipped += 1; continue
        title = re.search(r'<h3 class="mlc-itembox__title"[^>]*>(.*?)</h3>', c, re.S)
        params = {_txt(k).lower(): _txt(v) for k, v in re.findall(r'<div class="ml-text-small">\s*([^<:]+):\s*<span class="mlc-itembox__params__param__name">(.*?)</span>', c, re.S)}
        price = re.search(r'ml-offer-price__dollars">([^<]*)<', c)
        seller = re.search(r'Sprzedający:\s*(\w+)', c)
        addr = re.search(r'<address[^>]*>(.*?)</address>', c, re.S)
        img = re.search(r'<img src="([^"]+)"', c)
        meta = ld.get(url) or {}
        img_u = ((meta.get("image") or {}).get("url") if isinstance(meta.get("image"), dict) else None) or (img.group(1) if img else None)
        parts = [p.strip() for p in _txt(addr.group(1)).split(",") if p.strip()] if addr else []
        area = _money(params.get("powierzchnia", "")) if params.get("powierzchnia") else None
        rooms = re.search(r"\d+", params.get("liczba pokoi", "") or "")
        by = re.search(r"\b(1[89]\d{2}|20[0-2]\d)\b", params.get("rok budowy", "") or "")
        out.append({
            "id": path.rsplit("/", 1)[-1], "url": url, "title": _txt(title.group(1)) if title else (meta.get("name") or ""),
            "price": _money(price.group(1)) if price else _num((meta.get("offers") or {}).get("price")),
            "area": area, "rooms": int(rooms.group(0)) if rooms else None, "floor": None,
            "image": img_u, "district": parts[1] if len(parts) > 1 else None, "street": None,
            "loc0": parts[0] if parts else None, "desc": "", "market": (params.get("rynek") or "").lower() or None,
            "build_year": int(by.group(1)) if by else None,
            "seller": "posrednik" if seller and seller.group(1).lower().startswith("firm") else "prywatna",
        })
    if skipped: log.info("  allegrolokalnie: pominięto %d kart syndykowanych z innych portali", skipped)
    return out


PORTALS["okolica"] = {"url": url_okolica, "parse": parse_okolica}
PORTALS["krn"] = {"url": url_krn, "parse": parse_krn}
PORTALS["allegrolokalnie"] = {"url": url_allegrolokalnie, "parse": parse_allegrolokalnie}


# ───────────────────────── sprzedajemy.pl + lento.pl (16.11.2026) ─────────────────────────
def url_sprzedajemy(prop, trans, city, page):
    """sprzedajemy.pl — 30/str., sort=inp_srt_date_d (od najnowszych), offset. Karty mają datę dodania i typ sprzedawcy (OSOBA/FIRMA)."""
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "grunty-i-dzialki", "lokal": "lokale-uzytkowe"}[prop]
    loc = "" if city_slug(city) in ("polska", city.get("voi")) else f"/{city_slug(city)}"
    return f"https://sprzedajemy.pl{loc}/nieruchomosci/{seg}/{trans}?sort=inp_srt_date_d" + (f"&offset={(page - 1) * 30}" if page > 1 else "")


def _pl_dt_to_utc(s: str) -> Optional[str]:
    """„2026-11-16 13:59:15” (czas PL) → ISO UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_sprzedajemy(html: str) -> List[Dict[str, Any]]:
    out = []
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    for m in re.finditer(r'<li id="offer-(\d+)"[^>]*>\s*<article class="element[^"]*">(.*?)</article>', html, re.S):
        oid, c = m.group(1), m.group(2)
        link = re.search(r'<h2 class="title[^"]*">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', c, re.S)
        if not link: continue
        href = htmllib.unescape(link.group(1))
        price = re.search(r'class="price[^"]*">\s*([^<]*)<', c)
        area = re.search(r'Pow\.:\s*</span>\s*([\d\s.,]+)\s*m', c)
        seller = re.search(r'seller-type-info__label[^>]*>\s*(\w+)', c)
        when = re.search(r'<time class="time" datetime="([^"]+)"', c)
        city = re.search(r'<strong class="city">\s*(.*?)\s*</strong>', c, re.S)
        img = re.search(r'<img src="(https://thumbs\.img-sprzedajemy\.pl/[^"]+)"', c)
        pv = _money(price.group(1)) if price else None
        out.append({
            "id": oid, "url": "https://sprzedajemy.pl" + href if href.startswith("/") else href, "title": _txt(link.group(2)),
            "price": pv if pv and pv >= 500 else None,   # „1 zł” = cena do uzgodnienia
            "area": _money(area.group(1)) if area else None, "rooms": None, "floor": None,
            "image": img.group(1).replace("/350x250c/", "/1000x901c/") if img else None, "district": None, "street": None,
            "loc0": _txt(city.group(1)) if city else None, "desc": "",
            "seller": "prywatna" if seller and seller.group(1).upper() == "OSOBA" else ("posrednik" if seller else None),
            "posted_at": _pl_dt_to_utc(when.group(1)) if when else None,
        })
    return out


def url_lento(prop, trans, city, page):
    """lento.pl — tylko lista ogólnopolska (subdomeny miast odpowiadają 403 „nietypowe zachowanie”), ~40/str., od najnowszych."""
    if city_slug(city) not in ("polska", city.get("voi")):
        return None
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki-i-grunty", "lokal": "biura-i-lokale"}[prop]
    t = "do-wynajecia" if trans == "wynajem" else "sprzedaz"
    return f"https://www.lento.pl/nieruchomosci/{seg}/{t}.html" + (f"?page={page}" if page > 1 else "")


_PL_MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "paz": 10, "lis": 11, "gru": 12}


def _lento_date(s: str) -> Optional[str]:
    """„14 lis 10:29” (czas PL, rok bieżący; grudzień w styczniu → rok poprzedni) → ISO UTC."""
    s = re.sub(r"\s+", " ", s.lower()).strip()
    try:
        from zoneinfo import ZoneInfo
        from datetime import timedelta
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        hm = re.search(r"(\d{1,2}):(\d{2})", s)
        if s.startswith("dzisiaj") or s.startswith("wczoraj"):
            d = now - (timedelta(days=1) if s.startswith("wczoraj") else timedelta())
            if hm: d = d.replace(hour=int(hm.group(1)), minute=int(hm.group(2)), second=0, microsecond=0)
            return d.astimezone(timezone.utc).isoformat()
        m = re.search(r"(\d{1,2}) ([a-ząźż]{3})(?: (\d{1,2}):(\d{2}))?", s)
        if not m or m.group(2) not in _PL_MONTHS: return None
        d = now.replace(month=_PL_MONTHS[m.group(2)], day=int(m.group(1)), hour=int(m.group(3) or 12), minute=int(m.group(4) or 0), second=0, microsecond=0)
        if d > now: d = d.replace(year=d.year - 1)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_lento(html: str) -> List[Dict[str, Any]]:
    out = []
    html = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    for m in re.finditer(r'<div class="tablelist-tr[^"]*" data-id="(\d+)"[^>]*>(.*?)(?=<div class="tablelist-tr|<div class="tablelist-ads|<div class="pagination|</section>|$)', html, re.S):
        oid, c = m.group(1), m.group(2)
        link = re.search(r'<a href="([^"]+)" class="title-list-item">(.*?)</a>', c, re.S)
        if not link: continue
        city = re.search(r'licon-pin-f">([^<]*)<', c)
        price = re.search(r'price-list-item">([^<]*)<', c)
        when = re.search(r'<div class="data-list-item hidden-xs">(.*?)</div>', c, re.S)
        img = re.search(r'<img[^>]*(?:src|data-src)="(https://st-lento\.pl/adpics/[^"]+)"', c)
        if city and city.group(1).strip().lower().startswith("cała polska"): city = None
        desc = re.search(r'<p class="hidden-xs[^"]*">(.*?)</p>', c, re.S)
        params = [_txt(x) for x in re.findall(r'list-atrr-item-tab">(.*?)</span>', c, re.S)]
        area = next((_money(p) for p in params if re.search(r"m\s*(2|²)|\bar\b|\bha\b", p)), None)
        rooms = next((int(re.search(r"\d+", p).group(0)) for p in params if re.search(r"\d+\s*pok", p) and re.search(r"\d+", p)), None)
        out.append({
            "id": oid, "url": link.group(1), "title": _txt(re.sub(r"<span[^>]*>.*?</span>", "", link.group(2), flags=re.S)),
            "price": _money(price.group(1)) if price else None, "area": area, "rooms": rooms, "floor": None,
            "image": img.group(1).replace("/middle/", "/original/") if img else None, "district": None, "street": None,
            "loc0": _txt(city.group(1)) if city else None, "desc": _txt(desc.group(1)) if desc else "",
            "seller": "prywatna" if "bez pośredników" in c.lower() else "posrednik",
            "posted_at": _lento_date(_txt(when.group(1))) if when else None,
        })
    return out


PORTALS["sprzedajemy"] = {"url": url_sprzedajemy, "parse": parse_sprzedajemy}
PORTALS["lento"] = {"url": url_lento, "parse": parse_lento}


# ───────────────────────── 27.11.2026: homeportal.pl, domy.pl, klikmapa.pl, szybko.pl (prośba użytkownika) ─────────────────────────
def _pl_date(s: Optional[str]) -> Optional[str]:
    """„18-11-2026” / „18.11.2026” / „27 lis 2026” / „dzisiaj” / „wczoraj” → ISO UTC."""
    if not s: return None
    t = s.strip().lower()
    m = re.search(r"(\d{1,2})[.-](\d{1,2})[.-](\d{4})", t)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=timezone.utc).isoformat()
        except ValueError: return None
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]{3})\w*\s+(\d{4})", t)
    if m and m.group(2) in _PL_MONTHS:
        return datetime(int(m.group(3)), _PL_MONTHS[m.group(2)], int(m.group(1)), 12, tzinfo=timezone.utc).isoformat()
    return _rel_date(t)


def url_homeportal(prop, trans, city, page):
    """homeportal.pl — portal biur (agencje publikują bezpośrednio). Lista ogólnopolska SSR, 10 kart/str., sort=addedDesc."""
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "lokale"}[prop]
    if prop in ("dzialka", "lokal") and trans == "wynajem": return None
    return f"https://homeportal.pl/{trans}/{seg}/?sort=addedDesc" + (f"&page={page}" if page > 1 else "")


def parse_homeportal(html: str) -> List[Dict[str, Any]]:
    out = []
    for m in re.finditer(r'<article class="property-card.*?</article>', html, re.S):
        c = m.group(0)
        url = re.search(r'<a href="(/(?:sprzedaz|wynajem)/[^"]+/([A-Z0-9-]+)/)"', c)
        if not url: continue
        price = re.search(r'<h3[^>]*>([\d\s]+)\s*zł', c)
        area = re.search(r'Powierzchnia\s*([\d.,]+)\s*m', c)
        rooms = re.search(r'Ilość pokoi\s*(\d+)', c)
        by = re.search(r'Rok Budowy\s*(\d{4})', c)
        loc = re.search(r'<p class="text-xs text-gray-600 mt-1">([^<]*)</p>', c)
        img = re.search(r'<img src="([^"]+)"', c)
        added = re.search(r'leading-tight">\s*(\d{2}-\d{2}-\d{4})', c)
        agency = re.search(r'/agencies/([^/]+)/', c)
        parts = [p.strip() for p in _txt(loc.group(1)).split(",")] if loc else []
        # „lubelskie, Lublin, Śródmieście” → voi, miasto, dzielnica
        out.append({
            "id": url.group(2), "url": "https://homeportal.pl" + url.group(1),
            "title": "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": int(rooms.group(1)) if rooms else None, "floor": None, "build_year": int(by.group(1)) if by else None,
            "image": img.group(1) if img else None,
            "loc0": parts[1] if len(parts) > 1 else (parts[0] if parts else None),
            "district": parts[2] if len(parts) > 2 else None, "street": None,
            "desc": "", "seller": "posrednik", "agency": agency.group(1).replace("-", " ").title() if agency else None,
            "posted_at": _pl_date(added.group(1) if added else None),
        })
    for it in out:
        it["title"] = " ".join(x for x in [it.get("loc0"), it.get("district")] if x)
    return out


def url_domy(prop, trans, city, page):
    """domy.pl (grupa Morizon-Gratka) — lista ogólnopolska, 25/str., sort po dacie. Karty „domy-gratka-ID” to syndykacja z Gratki (pomijane)."""
    seg = {"mieszkanie": "mieszkania", "dom": "domy", "dzialka": "dzialki", "lokal": "lokale"}[prop]
    if prop in ("dzialka", "lokal") and trans == "wynajem": return None
    tr = "&ps%5Btransaction%5D=2" if trans == "wynajem" else ""
    return f"https://domy.pl/{seg}---pl?ps%5Bsort_order%5D=date&ps%5Bsort_asc%5D=0&limit=25&page={page}{tr}"


def parse_domy(html: str) -> List[Dict[str, Any]]:
    out = []
    for m in re.finditer(r'<article class="propertyBox.*?</article>', html, re.S):
        c = m.group(0)
        link = re.search(r'title="([^"]*)" class="property_link" href="(https://domy\.pl/[^"]+/dol(\d+))"', c)
        alt = re.search(r'<img alt="([^"]*)"', c)
        if not link: continue
        if alt and "domy-gratka-" in alt.group(1): continue   # ta sama oferta jest już z Gratki
        price = re.search(r'<span class="price">\s*([\d\s\xa0&nbsp;]+?)\s*(?:&nbsp;|\s)PLN', c)
        area = re.search(r'<span class="area">([\d\s,.]+)\s*m', c)
        floor = re.search(r'piętro:(?:&nbsp;|\s)*(\d+)', c)
        desc = re.search(r'<span class="descriptionShort">(.*?)</span>', c, re.S)
        lat = re.search(r'data-lat="([\d.]+)"', c); lng = re.search(r'data-lng="([\d.]+)"', c)
        added = re.search(r'addedAt">\s*data:\s*<strong>([^<]*)<', c)
        img = re.search(r'data-original="([^"]+)"', c)
        title = _txt(link.group(1))
        loc_txt = _txt(re.search(r'class="property_link"[^>]*>(.*?)</a>', c, re.S).group(1))
        parts = [p.strip() for p in loc_txt.split(",") if p.strip()]
        # „Warszawa, Bemowo, Lazurowa” / „mazowieckie, Warszawa, …” / „ostródzki, Ostróda, Jana Pawła II”
        if parts and (parts[0].islower() or re.search(r"(ski|cki|zki)$", parts[0])) and len(parts) > 1: parts = parts[1:]
        blob = (title + " " + (alt.group(1) if alt else "")).lower()
        seller = "deweloper" if "w inwestycji" in blob else ("prywatna" if re.search(r"bezpośrednio|od właściciela|bez pośredni", _txt(desc.group(1)).lower() if desc else "") else None)
        out.append({
            "id": link.group(3), "url": link.group(2), "title": title,
            "price": _money(price.group(1).replace("&nbsp;", "")) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": None, "floor": int(floor.group(1)) if floor else None,
            "image": img.group(1) if img else None, "loc0": parts[0] if parts else None,
            "district": parts[1] if len(parts) > 2 else None, "street": parts[-1] if len(parts) > 1 else None,
            "desc": _txt(desc.group(1)) if desc else "", "seller": seller,
            "lat": float(lat.group(1)) if lat else None, "lng": float(lng.group(1)) if lng else None,
            "posted_at": _pl_date(added.group(1) if added else None),
        })
    return out


def url_klikmapa(prop, trans, city, page):
    """klikmapa.pl (sieć beldom.pl) — Inertia: lista w atrybucie data-page (JSON), 12/str., domyślnie od najnowszych."""
    seg = {"mieszkanie": "mieszkanie", "dom": "dom", "dzialka": "dzialka", "lokal": "miejsce_uzytkowe"}[prop]
    if prop == "dzialka" and trans == "wynajem": return None
    return f"https://klikmapa.pl/oferty?offer_type={trans}&real_estate_type={seg}&page={page}"


def parse_klikmapa(html: str) -> List[Dict[str, Any]]:
    m = re.search(r'data-page="([^"]*)"', html)
    if not m: return []
    try:
        data = json.loads(htmllib.unescape(m.group(1)))["props"]["offers"]["data"]
    except Exception:
        return []
    out = []
    for o in data:
        if not o.get("url") or o.get("is_expired") or o.get("active") == 0: continue
        loc = o.get("location") or {}
        nm = lambda k: ((loc.get(k) or {}).get("name") or "").strip()
        coords = loc.get("coords") or {}
        price = (o.get("price") or {}).get("value")
        ut = o.get("user_type")
        seller = "prywatna" if ut == "private" else ("deweloper" if (o.get("business_profile") == "developer" or o.get("is_investment")) else "posrednik")
        out.append({
            "id": str(o["id"]), "url": o["url"], "title": re.sub(r"\b([A-ZĄĆĘŁŃÓŚŹŻ]{2,})\b", lambda mm: mm.group(1).title(), o.get("title") or ""),
            "price": float(price) if price else None, "area": float(o["object_size"]) if o.get("object_size") else None,
            "rooms": o.get("rooms"), "floor": None, "image": o.get("offer_thumbnail"),
            "loc0": nm("place").title() if nm("place") else None, "district": nm("district").title() or None, "street": nm("street").title() or None,
            "desc": "", "seller": seller, "market": {"1": "pierwotny", "2": "wtorny"}.get(str(o.get("market_type") or "")),
            "lat": float(coords["lat"]) if coords.get("lat") else None, "lng": float(coords["lon"]) if coords.get("lon") else None,
            "posted_at": o.get("created_at"),
        })
    return out


def url_szybko(prop, trans, city, page):
    """szybko.pl — portal biur i deweloperów. Lista ogólnopolska SSR (schema.org), 12/str., sort=newest, strona=N."""
    seg = {"mieszkanie": "lokal-mieszkalny+mieszkanie", "dom": "dom", "dzialka": "dzialka", "lokal": "nieruchomosci-komercyjne"}[prop]
    t = "na-wynajem" if trans == "wynajem" else "na-sprzedaz"
    return f"https://www.szybko.pl/l/{t}/{seg}?sort=newest" + (f"&strona={page}" if page > 1 else "")


def parse_szybko(html: str) -> List[Dict[str, Any]]:
    out = []
    for m in re.finditer(r'<div id="asset-\d+" data-assetid="(\d+)".*?class="listing-footer">.*?</div>\s*</div>', html, re.S):
        oid, c = m.group(1), m.group(0)
        url = re.search(r'<a href="(/o/[^"]+)"', c)
        if not url: continue
        price = re.search(r'itemprop="price" content="([\d.]+)"', c)
        title = re.search(r'class="listing-title-heading[^"]*"[^>]*>\s*([^<]*?)\s*</a>', c)
        addr = re.search(r'<i class="fa fa-map-marker"></i>\s*([^<]*?)\s*</a>', c)
        lat = re.search(r'data-lat="([\d.]+)"', c); lon = re.search(r'data-lon="([\d.]+)"', c)
        area = re.search(r'asset-feature area">([\d.,]+)\s*m', c)
        rooms = re.search(r'asset-feature rooms">(\d+)', c)
        by = re.search(r'asset-feature year_built">(\d{4})', c)
        desc = re.search(r'itemprop="description"[^>]*>(.*?)</span>', c, re.S)
        img = re.search(r'<link itemprop="image" href="([^"]+)"', c)
        added = re.search(r'asset-listing-date">.*?<span class="hidden-sm">\s*([^<]*?)\s*</span>', c, re.S)
        agent = re.search(r'class="asset-listing-owner">([^<]*)<', c)
        is_agent = bool(re.search(r'/posrednicy-nieruchomosci/|/deweloperzy/', c))
        # „Piastów (Mazowieckie, gm. Piastów)” / „Warszawa Wola (Mazowieckie, …)”
        a = re.sub(r"\(.*?\)", "", _txt(addr.group(1))).strip().rstrip(",") if addr else ""
        toks = [t.strip(",") for t in a.split() if t.strip(",")]
        city_name = " ".join(toks[:2]) if len(toks) > 1 and toks[1][0].isupper() and toks[0] in ("Nowy", "Nowa", "Nowe", "Zielona", "Gorzów", "Jelenia", "Dąbrowa", "Ruda", "Stalowa", "Tomaszów", "Piotrków", "Ostrów", "Bielsko", "Kędzierzyn") else (toks[0] if toks else None)
        out.append({
            "id": oid, "url": "https://www.szybko.pl" + url.group(1), "title": _txt(title.group(1)) if title else "",
            "price": float(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": int(rooms.group(1)) if rooms else None, "floor": None, "build_year": int(by.group(1)) if by else None,
            "image": img.group(1) if img else None, "loc0": city_name,
            "district": " ".join(toks[len(city_name.split()):]) or None if city_name else None, "street": None,
            "desc": _txt(desc.group(1)) if desc else "", "seller": "posrednik" if is_agent else None,
            "agency": None,   # w stopce karty jest agent (osoba), nie nazwa biura
            "lat": float(lat.group(1)) if lat else None, "lng": float(lon.group(1)) if lon else None,
            "posted_at": _pl_date(added.group(1) if added else None),
        })
    return out


def url_domplace(prop, trans, city, page):
    """domplace.pl (Nuxt) — publiczne JSON API /api/search?sort=newest (wszystkie kategorie w jednej liście, 50/str.).
    Filtrowanie rodzaj/transakcja robi parser (propertyCategory, listingType) — dlatego tylko kombinacja mieszkanie/sprzedaz
    pobiera listę, pozostałe zwracają None (jeden przebieg importuje wszystko)."""
    if not (prop == "mieszkanie" and trans == "sprzedaz"): return None
    return f"https://domplace.pl/api/search?sort=newest&pageSize=50&page={page}"


_DP_CAT = {0: ("mieszkanie", "mieszkanie"), 1: ("dom", "dom"), 2: ("dzialka", "działka"), 3: ("lokal", "lokal"), 4: ("lokal", "lokal")}


def parse_domplace(html: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html).get("items") or []
    except Exception:
        return []
    out = []
    for o in data:
        if o.get("isDemo") or o.get("isHidden") or o.get("currency", "PLN") != "PLN": continue
        cat = _DP_CAT.get(o.get("propertyCategory"))
        if not cat: continue
        loc = [x.strip() for x in str(o.get("location") or "").split(",") if x.strip()]
        city = loc[0].title() if loc and loc[0].isupper() else (loc[0] if loc else None)
        district = loc[1].title() if len(loc) > 1 and not loc[1].lower().startswith(("ul.", "al.", "os.")) and not loc[1].endswith("ski") and not loc[1].endswith("cki") else None
        street = next((x for x in loc[1:] if x.lower().startswith(("ul.", "al.", "os.", "pl."))), None)
        ct = o.get("ownerCompanyType")
        seller = "deweloper" if ct == "developer" else ("posrednik" if ct in ("agent", "company") or o.get("ownerCompanyName") else "prywatna")
        photos = [("https://domplace.pl" + p["url"]) if str(p.get("url", "")).startswith("/") else p.get("url") for p in (o.get("photos") or []) if p.get("url")]
        desc = re.sub(r"<[^>]+>", " ", str(o.get("description") or ""))
        out.append({
            "id": str(o["id"]), "url": f"https://domplace.pl/ogloszenie/{_slug(o.get('title') or 'oferta')}-{o['id']}", "title": (o.get("title") or "").strip(),
            "price": float(o["price"]) if o.get("price") else None, "area": o.get("areaM2") or o.get("plotArea"), "rooms": o.get("rooms"),
            "floor": o.get("floor"), "build_year": o.get("buildYear"), "image": photos[0] if photos else None, "images": photos[:12],
            "loc0": city, "district": district, "street": street, "desc": _txt(desc)[:1500], "seller": seller,
            "agency": o.get("ownerCompanyName") or None, "lat": o.get("latitude"), "lng": o.get("longitude"),
            "posted_at": (o.get("createdAt") or "")[:19] + "+00:00" if o.get("createdAt") else None,
            "market": {0: "pierwotny", 1: "pierwotny", 2: "wtorny"}.get(o.get("finishState")) if cat[0] in ("mieszkanie", "dom") and o.get("finishState") in (0, 1) else None,
            "_prop": cat[0], "_trans": "wynajem" if (o.get("listingType") == 1 or (o.get("price") or 0) < 20000) else "sprzedaz",
        })
    return out


def _slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower()).encode("ascii", "ignore").decode().replace("ł", "l")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")[:80] or "oferta"


PORTALS["domplace"] = {"url": url_domplace, "parse": parse_domplace}
PORTALS["homeportal"] = {"url": url_homeportal, "parse": parse_homeportal}
PORTALS["domy"] = {"url": url_domy, "parse": parse_domy}
PORTALS["klikmapa"] = {"url": url_klikmapa, "parse": parse_klikmapa}
PORTALS["szybko"] = {"url": url_szybko, "parse": parse_szybko}
NATIONAL_LIST_PORTALS |= {"homeportal", "domy", "klikmapa", "szybko", "domplace"}
CFFI_PORTALS |= {"sprzedajemy", "lento"}
NATIONAL_LIST_PORTALS |= {"sprzedajemy", "lento"}
_CFFI_SESSION = None


def _cffi_session():
    """Jedna sesja (ciasteczka) — lento.pl blokuje „zimne” wejścia na listy bez wcześniejszej wizyty na stronie głównej."""
    global _CFFI_SESSION
    if _CFFI_SESSION is None and cffi_requests is not None:
        _CFFI_SESSION = cffi_requests.Session(impersonate="chrome")
        try:
            _CFFI_SESSION.get("https://www.lento.pl/", timeout=30); time.sleep(2)
        except Exception as e:
            log.warning("  lento warm-up: %s", str(e)[:80])
    return _CFFI_SESSION


def fetch_cffi(url: str) -> Optional[str]:
    """Pobranie z odciskiem TLS/HTTP2 Chrome (Cloudflare przed okolica/krn). krn.pl zrywa co kilka połączeń → ponawiamy z pauzą."""
    if cffi_requests is None:
        log.error("curl_cffi niezainstalowane"); return None
    for i in range(4):
        try:
            if "lento.pl" in url:
                r = _cffi_session().get(url, timeout=40, headers={"Accept-Language": "pl-PL,pl;q=0.9", "Referer": "https://www.lento.pl/"})
            else:
                r = cffi_requests.get(url, impersonate="chrome", timeout=40, headers={"Accept-Language": "pl-PL,pl;q=0.9"})
            if r.status_code == 200 and len(r.text) > 3000: return r.text
            if r.status_code == 404: return None
            log.warning("  HTTP %s — pauza %ds", r.status_code, 10 * (i + 1)); time.sleep(10 * (i + 1))
        except Exception as e:
            log.warning("  fetch_cffi error: %s", str(e)[:100]); time.sleep(4 * (i + 1))
    return None


def fetch(url: str) -> Optional[str]:
    if any(h in url for h in ("okolica.pl", "krn.pl", "allegrolokalnie.pl", "sprzedajemy.pl", "lento.pl")):
        return fetch_cffi(url)
    for _ in range(3):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 3000:
                if "mojelokum.pl" in url: r.encoding = "utf-8"   # serwer nie podaje charsetu w nagłówku → requests zgaduje ISO-8859-1 (krzaczki w „zł”, nazwach)
                return r.text
            if r.status_code == 404: return None
            if r.status_code in (403, 429): log.warning("  HTTP %s — pauza 20s", r.status_code); time.sleep(20)
        except requests.RequestException as e:
            log.warning("  fetch error: %s", str(e)[:100]); time.sleep(3)
    return None


def guess_city(portal: str, it: Dict[str, Any]) -> Optional[str]:
    """Tryb województw: miasto z URL/tytułu oferty."""
    u = it.get("url") or ""
    if portal == "morizon":
        m = re.search(r"/oferta/(?:sprzedaz|wynajem)-(?:hale-magazyny|handel-uslugi|biura-lokale|lokale-uzytkowe|[a-z_]+)-([a-z0-9-]+?)(?:-\d+m2)?-mzn\d+", u)
        if m:
            toks = m.group(1).split("-")
            # poza dużymi miastami Morizon daje „powiat-miejscowość” (np. piaseczynski-piaseczno) → bierzemy miejscowość
            if len(toks) >= 2 and re.search(r"(ski|cki|zki)$", toks[0]) and toks[0] not in ("mikolajki",):
                toks = toks[1:]
            return city_from_slug(toks)
    elif portal == "gratka":
        m = re.search(r"/nieruchomosci/(?:mieszkanie|dom|dzialka|lokal)-([a-z0-9-]+)/ob/", u)
        if m: return city_from_slug(m.group(1).split("-"))
    elif portal == "domiporta":
        t = (it.get("loc0") or "")
        parts = t.split(" ", 1)
        if len(parts) == 2: return parts[1].strip().title()
    elif portal in ("gethome", "odwlasciciela"):
        t = (it.get("loc0") or "").strip()
        return t.title() if t and t.islower() else (t or None)
    elif portal == "adresowo":
        t = (it.get("loc0") or "").strip()
        toks = t.split(" ")
        two = " ".join(toks[:2])
        if two in {"Nowy Sącz", "Zielona Góra", "Gorzów Wielkopolski", "Jelenia Góra", "Dąbrowa Górnicza", "Ruda Śląska", "Stalowa Wola", "Nowy Targ", "Tomaszów Mazowiecki", "Piotrków Trybunalski", "Ostrów Wielkopolski", "Bielsko-Biała"}: return two
        return toks[0] if toks and toks[0] else None
    elif portal == "listaprzetargow":
        return (it.get("loc0") or "").strip() or None   # „Radom, Południe” → Radom
    elif portal in ("okolica", "krn", "allegrolokalnie", "sprzedajemy", "lento", "homeportal", "domy", "klikmapa", "szybko", "domplace", "mojelokum"):
        t = (it.get("loc0") or "").strip()              # okolica: „Szczecin, Dąbie, ul. X”; krn: addressLocality; allegro: <address>
        return (t.title() if t.islower() else t) or None
    return None


def normalize(portal: str, it: Dict[str, Any], city_name: str, prop: str, trans: str) -> Dict[str, Any]:
    prop, trans = it.get("_prop") or prop, it.get("_trans") or trans   # domplace: lista mieszana — rodzaj/transakcja z karty
    if city_name in ("Polska",) or city_name.islower():   # tryb regionów (nazwa województwa) / cała Polska
        city_name = guess_city(portal, it) or city_name
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
        "rooms": int(it["rooms"]) if it.get("rooms") else infer_rooms(it.get("title"), it.get("desc")), "floor": it.get("floor"),
        "image": it.get("image"), "images": (it.get("images") or ([it["image"]] if it.get("image") else [])),
        "description": (it.get("desc") or "")[:2000], "transaction_type": trans, "seller_type": seller,
        "last_seen_at": now_iso, "scraped_via": "portal-direct",
    }
    if subtype: doc["dzialka_type" if prop == "dzialka" else "building_type"] = subtype
    # 🏗️ rynek pierwotny/wtórny od razu przy imporcie (nowe oferty bez market_type nie trafiały do filtra „pierwotny”)
    mt = infer_market(it.get("market"), doc["title"], doc["description"], it.get("build_year"), seller, ptype)
    if mt: doc["market_type"] = mt
    if portal == "adresowo": doc["seller_verified"] = True   # seller z etykiety karty („bez pośredników”)
    # 🏢 Nazwa biura/dewelopera (gdy portal ją podaje w danych listy)
    an = clean_agency(it.get("agency")) if seller != "prywatna" else None
    if an: doc["agency_name"] = an
    for k in ("lat", "lng", "plot_area_m2", "phone", "build_year", "posted_at", "auction_kind", "auction_deadline", "auction_discount_pct"):
        if it.get(k) not in (None, "", 0): doc[k] = it[k]
    # 📅 rok budowy z opisu („w budynku z 1976”, „rok budowy: 2009”) — listy portali nie mają tego pola
    if "build_year" not in doc and prop in ("mieszkanie", "dom", "lokal"):
        by = infer_build_year(doc["title"], it.get("desc"))
        if by: doc["build_year"], doc["build_year_inferred"] = by, True
    # 🌳 wielkość działki z opisu (Gratka/Morizon/Domiporta nie podają jej w danych listy)
    if "plot_area_m2" not in doc and prop in ("dom", "dzialka"):
        m = re.search(r"dzia[łl]k[aięą]\w*\D{0,30}?(\d[\d\s.,]{1,8})\s*(m2|m²|mkw|m kw|ar(?:y|ów)?|ha)\b", blob)
        if m:
            try:
                v = float(m.group(1).replace(" ", "").replace(",", ".").rstrip("."))
                unit = m.group(2)
                v = v * 100 if unit.startswith("ar") else v * 10000 if unit == "ha" else v
                if 30 <= v <= 5_000_000: doc["plot_area_m2"] = round(v, 1)
            except ValueError:
                pass
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
    ap.add_argument("--regions", action="store_true", help="16 województw (adresowo: cała Polska) zamiast 20 miast")
    a = ap.parse_args()
    P = PORTALS[a.portal]
    db = get_mongo(); coll = db["listings"]
    cities = CITIES if not a.city else [c for c in CITIES if city_slug(c) == a.city.lower()]
    if a.regions and not a.city: cities = [{"name": "Polska", "slug": "polska", "voi": "polska"}] if a.portal in NATIONAL_LIST_PORTALS else REGIONS
    if a.portal == "odwlasciciela": a.max_pages = max(a.max_pages, 3)   # mały portal — 20 ofert/strona
    if a.portal == "listaprzetargow": a.max_pages = max(a.max_pages, 4)  # lista ogólnopolska, 25/str. — 4 strony = ~100 najnowszych
    if a.portal in ("okolica", "krn", "sprzedajemy", "lento"): a.max_pages = max(a.max_pages, 4)   # listy ogólnopolskie 25–40/str.
    if a.portal in ("homeportal", "klikmapa", "szybko"): a.max_pages = max(a.max_pages, 4)         # 10–12 kart/str. (27.11)
    if a.portal == "domplace": a.max_pages = max(a.max_pages, 3)                                     # 50/str., wszystkie kategorie naraz
    if a.portal == "domy": a.max_pages = max(a.max_pages, 3); a.delay = max(a.delay, 2.0)           # 25/str.; zrywa połączenia przy szybkich zapytaniach
    if a.portal == "allegrolokalnie": a.max_pages = max(a.max_pages, 6)    # 60/str., ale ~90% kart to syndykacja z Gratki (pomijana)
    if a.portal in CFFI_PORTALS: a.delay = max(a.delay, 2.5)                # krn.pl zrywa połączenia przy szybkich zapytaniach
    if not cities: log.error("Unknown city"); sys.exit(1)
    log.info("═══ %s — %s/%s — cities=%d max_pages=%d ═══", a.portal, a.transaction, a.property, len(cities), a.max_pages)
    tot = {"new": 0, "updated": 0, "pages": 0}
    for city in cities:
        log.info("── %s ──", city["name"])
        # 🔎 tryb głęboki (max_pages ≥ 30 — jedyny parametr dostępny z workflow_dispatch): nie przerywaj po 1 stronie bez nowych —
# listy „od najnowszych” mają w środku strony już zaimportowane, a za nimi starsze oferty, których nigdy nie pobraliśmy (brak duplikatów).
        deep = a.max_pages >= 30; zero_streak = 0
        for page in range(1, a.max_pages + 1):
            u = P["url"](a.property, a.transaction, city, page)
            if not u: log.info("  %s: portal nie ma kategorii %s", a.portal, a.property); break
            html = fetch(u); tot["pages"] += 1
            if not html: log.info("  page %d: brak HTML", page); break
            items = P["parse"](html)
            if not items:
                if a.portal == "allegrolokalnie" and "mlc-itembox" in html:   # strona pełna kart syndykowanych (pomijanych) — idziemy dalej
                    zero_streak += 1; log.info("  page %d: 0 własnych ofert", page)
                    if zero_streak >= (6 if deep else 3): break
                    time.sleep(a.delay); continue
                log.info("  page %d: 0 ofert", page); break
            ops = []
            if a.portal in ("odwlasciciela", "mojelokum"):   # numer właściciela ze strony oferty — dla ofert prywatnych bez numeru w bazie (nowe i stare)
                have_phone = {x["external_id"] for x in coll.find({"source": a.portal, "external_id": {"$in": [str(it["id"]) for it in items]}, "phone": {"$regex": r"\d{9}"}}, {"external_id": 1})}
                for it in items:
                    if a.portal == "mojelokum" and it.get("seller") != "prywatna": continue
                    if str(it["id"]) not in have_phone and tot.setdefault("phones", 0) < ODW_PHONE_LIMIT:
                        ph = enrich_odwlasciciela_phone(it["url"]) if a.portal == "odwlasciciela" else enrich_mojelokum_phone(it["url"]); time.sleep(0.8)
                        if ph: it["phone"] = ph; tot["phones"] += 1
            docs_batch = [d for d in (normalize(a.portal, it, city["name"], a.property, a.transaction) for it in items) if d["price"]]
            ph = price_history_ops(coll, a.portal, docs_batch)
            if ph: log.info("  📈 zmiany cen: %d", len(ph))
            for d in docs_batch:
                posted = d.pop("posted_at", None)
                ops.append(UpdateOne({"source": a.portal, "external_id": d["external_id"]},
                                     with_price_history({"$set": d, "$setOnInsert": {"added_at": posted or d["last_seen_at"], "posted_at": posted},
                                      "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}}, ph.get(d["external_id"])), upsert=True))
            new = upd = 0
            if ops:
                try:
                    r = coll.bulk_write(ops, ordered=False); new, upd = r.upserted_count, r.modified_count
                except Exception as e:
                    log.warning("  bulk_write: %s", str(e)[:200])
            tot["new"] += new; tot["updated"] += upd
            log.info("  page %d: %d ofert → new=%d upd=%d (🏢 z nazwą biura: %d)", page, len(items), new, upd, sum(1 for d in docs_batch if d.get("agency_name")))
            zero_streak = zero_streak + 1 if new == 0 else 0
            if new == 0 and page >= 2 and zero_streak >= (4 if deep else 2): log.info("  → brak nowych, następne miasto"); break
            time.sleep(a.delay)
    log.info("═══ DONE %s %s/%s: new=%d updated=%d pages=%d ═══", a.portal, a.transaction, a.property, tot["new"], tot["updated"], tot["pages"])
    if a.portal == "adresowo":
        _verify_adresowo_sellers(coll, a.property, a.transaction)
        if a.property == "mieszkanie" and a.transaction == "sprzedaz":
            # 🌍 Zagranica (realting.com: ES/IT/US/PT) — co turę 2 najnowsze strony na kraj/kategorię; pełny import: --max-pages ≥ 30 → 12 stron
            try:
                from scrape_zagranica import run as _zagr_run, run_greenacres as _ga_run, mark_stale as _zagr_stale, backfill_flags as _zagr_flags, fix_areas as _zagr_fix
                fc = coll.database["listings_foreign"]; deep_z = a.max_pages >= 30
                _zagr_run(pages=12 if deep_z else 2, coll=fc); _ga_run(pages=15 if deep_z else 2, coll=fc); _zagr_fix(fc); _zagr_flags(fc); _zagr_stale(fc)   # landelity wyłączone (brak zdjęć)
            except Exception as e:
                log.error("zagranica failed: %s", str(e)[:200])
    # Galerie (do 12 zdjęć) ze stron najnowszych ofert bez galerii — ekstraktory per portal w scripts/gallery.py. 22.11
    try:
        from gallery import enrich_galleries as _enrich_galleries
        _enrich_galleries(coll, a.portal, log)
    except Exception as e:
        log.error("galerie failed: %s", str(e)[:200])
    # Rewalidacja najstarszych ofert tego portalu (zdjęte → is_stale). Reguły per portal w scripts/stale_check.py. 20.11
    try:
        from stale_check import sweep as _stale_sweep
        _stale_sweep(coll, a.portal, log)
    except Exception as e:
        log.error("stale-sweep failed: %s", str(e)[:200])
    # ⏰ Odświeżanie co 30 min (fresh_sweep): lekki job portali może zostać liderem następnej tury (claim w Mongo). 27.11
    if a.regions and a.max_pages <= 2 and a.portal in ("morizon", "gratka", "domiporta", "adresowo"):
        try:
            import fresh_sweep
            fresh_sweep.wait_and_dispatch_next(db, _JOB_STARTED_AT, who=f"portals:{a.portal}/{a.property}")
        except Exception as e:
            log.warning("fresh sweep loop skipped: %s", e)


ODW_PHONE_LIMIT = int(os.environ.get("ODW_PHONE_LIMIT", "150"))
ADRESOWO_VERIFY_LIMIT = int(os.environ.get("ADRESOWO_VERIFY_LIMIT", "120"))


def _verify_adresowo_sellers(coll, prop: str, trans: str) -> None:
    """Do 5.11.2026 wszystkie oferty adresowo były zapisywane jako „prywatna” — ok. 40% to agencje.
    Weryfikacja istniejących rekordów (bez flagi seller_verified) ze strony oferty, ADRESOWO_VERIFY_LIMIT/run."""
    q = {"source": "adresowo", "seller_type": "prywatna", "seller_verified": {"$ne": True}, "is_stale": {"$ne": True},
         "transaction_type": trans, "type": {"$regex": "^" + re.escape(BASE_LABEL[prop])}}
    fixed = checked = 0
    for d in coll.find(q, {"url": 1}).limit(ADRESOWO_VERIFY_LIMIT):
        s = verify_adresowo_seller(d["url"]); time.sleep(0.8)
        if not s: continue
        checked += 1
        upd = {"seller_verified": True}
        if s != "prywatna": upd["seller_type"] = s; fixed += 1
        coll.update_one({"_id": d["_id"]}, {"$set": upd})
    log.info("═══ adresowo seller-verify %s/%s: checked=%d → posrednik=%d ═══", trans, prop, checked, fixed)


# ── Lokale użytkowe: matryce w .github/workflows nie mają „lokal” (nie da się ich edytować bez uprawnienia „Workflows”
#    na tokenie GitHub), więc lokal (sprzedaż + wynajem) jedzie w tym samym zadaniu co działki. 2.11.2026
CHAIN_AFTER = {"dzialka": [("lokal", "sprzedaz"), ("lokal", "wynajem")]}
# Nowe portale (5.11.2026) jadą w zadaniach istniejących portali (ta sama property/transaction):
# ───────────────────────── 28.11.2026: mojelokum.pl (lokalny portal — Pszczyna i okolice, pless.pl) ─────────────────────────
# URL listy: /{strona},{kategoria 1 mieszkanie|2 dom|3 działka|4 lokal},0,0,0,{typ 1 sprzedam|3 wynajem},0,…,{dodano_przez idx14},{sort idx15 = 1 najnowsze},0,0,{rynek},lista,ogloszenia.html
# 30 kart/str., kilka stron na kategorię. Kto dodał (osoba prywatna / firma / agencja) nie jest na karcie — pobieramy raz listę
# „osoba prywatna” (dodano_przez=1) dla wszystkich kategorii i oznaczamy po ID; reszta = pośrednik (firma/agencja).
_ML_KAT = {"mieszkanie": 1, "dom": 2, "dzialka": 3, "lokal": 4}
_ML_PRIVATE_IDS: Optional[set] = None


def _ml_url(kat: int, typ: int, page: int, dp: int = 0) -> str:
    f = ["0"] * 19
    f[0], f[1], f[5], f[14], f[15] = str(page), str(kat), str(typ), str(dp), "1"
    return "https://www.mojelokum.pl/" + ",".join(f) + ",lista,ogloszenia.html"


def url_mojelokum(prop, trans, city, page):
    return _ml_url(_ML_KAT[prop], 3 if trans == "wynajem" else 1, page)


def _ml_private_ids() -> set:
    global _ML_PRIVATE_IDS
    if _ML_PRIVATE_IDS is None:
        ids = set()
        for kat in (1, 2, 3, 4):
            for typ in (1, 3):
                for pg in (1, 2, 3):
                    h = fetch(_ml_url(kat, typ, pg, dp=1))
                    found = re.findall(r'<article id="o(\d+)" class="zajawka">', h or "")
                    ids.update(found)
                    if len(found) < 30: break
        _ML_PRIVATE_IDS = ids
        log.info("  mojelokum: %d ogłoszeń od osób prywatnych", len(ids))
    return _ML_PRIVATE_IDS


def parse_mojelokum(html: str) -> List[Dict[str, Any]]:
    out = []
    priv = _ml_private_ids()
    for m in re.finditer(r'<article id="o(\d+)" class="zajawka">(.*?)</article>', html, re.S):
        oid, c = m.group(1), m.group(2)
        title = re.search(r"<h3>([^<]*)</h3>", c)
        price = re.search(r'<span class="cena">([\d\s\xa0.,]+?)\s*zł', c)
        area = re.search(r"</span>,\s*([\d\s.,]+)\s*m<sup>2", c)
        loc = re.search(r'<span class="miejscowosc">([^<]*)</span>', c)
        desc = re.search(r"</h3>\s*<p>(.*?)</p>", c, re.S)
        date = re.search(r'<span class="data">[^:<]*:\s*([^<]*)</span>', c)
        imgs = re.findall(r'<var href="(/ib/big/[^"]+)"', c)
        t = _txt(title.group(1)) if title else ""
        rooms = re.search(r"(\d)\s*pokoj", t) or re.search(r"(\d)\s*pok", t)
        if "kawalerka" in t.lower(): rooms_n = 1
        else: rooms_n = int(rooms.group(1)) if rooms else None
        lp = [x.strip() for x in _txt(loc.group(1)).split(" - ", 1)] if loc else []
        trans = "wynajem" if t.lower().startswith("mam do wynaj") else ("sprzedaz" if t.lower().startswith("sprzedam") else None)
        if trans is None: continue   # „Kupię” / „Chcę wynająć” = poszukiwania, nie oferty
        kind = t.split(":", 1)[1].strip() if ":" in t else t      # „Mieszkanie - 3 pokoje” / „Działka - budowlana”
        nice = " ".join(x for x in [kind.replace(" - ", ", "), f"{area.group(1).strip()} m²" if area else "", "—", ", ".join(lp)] if x).strip(" —")
        out.append({
            "id": oid, "url": f"https://www.mojelokum.pl/0,0,0,0,0,0,0,0,{oid},0,0,0,0,0,0,0,0,0,0,ogloszenie,ogloszenia.html",
            "title": nice or t, "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": rooms_n, "floor": None, "build_year": None,
            "image": ("https://www.mojelokum.pl" + imgs[0]) if imgs else None,
            "images": ["https://www.mojelokum.pl" + u for u in imgs[:12]],
            "loc0": lp[0] if lp else None, "district": lp[1] if len(lp) > 1 else None, "street": None,
            "desc": _txt(desc.group(1)) if desc else "",
            "seller": "prywatna" if oid in priv else "posrednik", "agency": None,
            "posted_at": _pl_date(date.group(1) if date else None), "_trans": trans,
        })
    return out


def enrich_mojelokum_phone(url: str) -> Optional[str]:
    """Strona oferty mojelokum.pl: „tel. 661835929” w bloku kontaktu ogłoszeniodawcy."""
    html = fetch(url)
    if not html: return None
    m = re.search(r"tel\.?\s*:?\s*(?:\+?48)?\s*((?:\d[\s-]?){9})", html, re.I)
    if not m: return None
    digits = re.sub(r"\D", "", m.group(1))
    return "+48" + digits if len(digits) == 9 else None


PORTALS["mojelokum"] = {"url": url_mojelokum, "parse": parse_mojelokum}
NATIONAL_LIST_PORTALS |= {"mojelokum"}

PORTAL_CHAIN = {"adresowo": ["odwlasciciela", "allegrolokalnie", "szybko"], "domiporta": ["gethome", "krn", "klikmapa", "mojelokum"],
                "gratka": ["listaprzetargow", "sprzedajemy", "domy"], "morizon": ["okolica", "lento", "homeportal", "domplace"]}
# listaprzetargow (5.11): przetargi i licytacje, badge 🔨; okolica/krn/allegrolokalnie (16.11); homeportal/domy/klikmapa/szybko (27.11)


def _run_portal_chain(argv0: List[str]) -> None:
    if "--portal" not in argv0:
        return
    for p2 in PORTAL_CHAIN.get(argv0[argv0.index("--portal") + 1], []):
        nxt = list(argv0); nxt[nxt.index("--portal") + 1] = p2
        sys.argv = [sys.argv[0]] + nxt
        log.info("═══ PORTAL CHAIN → %s ═══", p2)
        try:
            main()
        except SystemExit:
            pass
        except Exception as e:
            log.error("PORTAL CHAIN %s failed: %s", p2, str(e)[:200])


def _run_with_chain():
    main()
    argv = sys.argv[1:]
    _run_portal_chain(argv)
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
        _run_portal_chain(nxt)


if __name__ == "__main__":
    _run_with_chain()
