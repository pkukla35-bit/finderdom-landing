#!/usr/bin/env python3
"""
Darmowy scraper (bez proxy, 0 zł) dla portali: morizon, gratka, domiporta, adresowo → MongoDB `listings`.
Strony list są renderowane serwerowo; pobieramy tylko listy (bez szczegółów), więc 1 zapytanie = 35–40 ofert.

Użycie:
  python scripts/scrape_portals.py --portal morizon --property mieszkanie --transaction sprzedaz [--city krakow] [--max-pages 5]
Env: MONGODB_URI (lub MONGO_URL), MONGODB_DB
"""
import argparse, html as htmllib, json, logging, os, re, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_otodom import CITIES, REGIONS, DOM_SUBTYPES, DZIALKA_SUBTYPES, _num, city_from_slug, clean_agency, price_history_ops, with_price_history  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("portals")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
S = requests.Session()
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
        mid = re.search(r'data-detail-id="(\d+)"', a)
        href = re.search(r'href="(/nieruchomosci/[^"]+/\d+)"', a)
        if not mid or not href: continue
        title = re.search(r'<h2 class="sneakpeak__title--bold">(.*?)</h2>', a, re.S)
        loc = re.search(r'<span class="sneakpeak__title--inblock">(.*?)</span>', a, re.S)
        price = re.search(r'<span class="sneakpeak__price_value">(.*?)</span>', a, re.S)
        area = re.search(r'sneakpeak__details_item--area"[^>]*>(.*?)</span>', a, re.S)
        rooms = re.search(r'sneakpeak__details_item--room"[^>]*>(.*?)</span>', a, re.S)
        img = re.search(r'<img[^>]+src="(https://galeria\.domiporta\.pl/[^"]+)"', a)
        desc = re.search(r'<p class="sneakpeak__[a-z_]*"[^>]*>([^<]{5,})', a)
        loc_parts = [p.strip() for p in _txt(loc.group(1)).split(",")] if loc else []
        out.append({
            "id": mid.group(1), "url": "https://www.domiporta.pl" + href.group(1), "title": _txt(title.group(1)) if title else "",
            "price": _money(price.group(1)) if price else None, "area": _money(area.group(1)) if area else None,
            "rooms": _num(_txt(rooms.group(1))) if rooms else None, "floor": None, "image": img.group(1) if img else None,
            "district": loc_parts[1] if len(loc_parts) > 1 else None, "street": loc_parts[2] if len(loc_parts) > 2 else None,
            "loc0": loc_parts[0] if loc_parts else None,
            "desc": _txt(desc.group(1)) if desc else "", "seller": None,
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


def fetch(url: str) -> Optional[str]:
    for _ in range(3):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 3000: return r.text
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
    return None


def normalize(portal: str, it: Dict[str, Any], city_name: str, prop: str, trans: str) -> Dict[str, Any]:
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
        "rooms": int(it["rooms"]) if it.get("rooms") else None, "floor": it.get("floor"),
        "image": it.get("image"), "images": [it["image"]] if it.get("image") else [],
        "description": (it.get("desc") or "")[:2000], "transaction_type": trans, "seller_type": seller,
        "last_seen_at": now_iso, "scraped_via": "portal-direct",
    }
    if subtype: doc["dzialka_type" if prop == "dzialka" else "building_type"] = subtype
    if portal == "adresowo": doc["seller_verified"] = True   # seller z etykiety karty („bez pośredników”)
    # 🏢 Nazwa biura/dewelopera (gdy portal ją podaje w danych listy)
    an = clean_agency(it.get("agency")) if seller != "prywatna" else None
    if an: doc["agency_name"] = an
    for k in ("lat", "lng", "plot_area_m2", "phone", "build_year", "posted_at", "auction_kind", "auction_deadline", "auction_discount_pct"):
        if it.get(k) not in (None, "", 0): doc[k] = it[k]
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
    if a.regions: cities = [{"name": "Polska", "slug": "polska", "voi": "polska"}] if a.portal in ("adresowo", "listaprzetargow") else REGIONS
    if a.portal == "odwlasciciela": a.max_pages = max(a.max_pages, 3)   # mały portal — 20 ofert/strona
    if a.portal == "listaprzetargow": a.max_pages = max(a.max_pages, 4)  # lista ogólnopolska, 25/str. — 4 strony = ~100 najnowszych
    if not cities: log.error("Unknown city"); sys.exit(1)
    log.info("═══ %s — %s/%s — cities=%d max_pages=%d ═══", a.portal, a.transaction, a.property, len(cities), a.max_pages)
    tot = {"new": 0, "updated": 0, "pages": 0}
    for city in cities:
        log.info("── %s ──", city["name"])
        for page in range(1, a.max_pages + 1):
            u = P["url"](a.property, a.transaction, city, page)
            if not u: log.info("  %s: portal nie ma kategorii %s", a.portal, a.property); break
            html = fetch(u); tot["pages"] += 1
            if not html: log.info("  page %d: brak HTML", page); break
            items = P["parse"](html)
            if not items: log.info("  page %d: 0 ofert", page); break
            ops = []
            if a.portal == "odwlasciciela":   # numer właściciela ze strony oferty — dla ofert, które nie mają jeszcze numeru w bazie (nowe i stare)
                have_phone = {x["external_id"] for x in coll.find({"source": a.portal, "external_id": {"$in": [str(it["id"]) for it in items]}, "phone": {"$regex": r"\d{9}"}}, {"external_id": 1})}
                for it in items:
                    if str(it["id"]) not in have_phone and tot.setdefault("phones", 0) < ODW_PHONE_LIMIT:
                        ph = enrich_odwlasciciela_phone(it["url"]); time.sleep(0.8)
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
            if new == 0 and page >= 2: log.info("  → brak nowych, następne miasto"); break
            time.sleep(a.delay)
    log.info("═══ DONE %s %s/%s: new=%d updated=%d pages=%d ═══", a.portal, a.transaction, a.property, tot["new"], tot["updated"], tot["pages"])
    if a.portal == "adresowo":
        _verify_adresowo_sellers(coll, a.property, a.transaction)


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
PORTAL_CHAIN = {"adresowo": ["odwlasciciela"], "domiporta": ["gethome"], "gratka": ["listaprzetargow"]}   # listaprzetargow (5.11): przetargi i licytacje, badge 🔨


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
