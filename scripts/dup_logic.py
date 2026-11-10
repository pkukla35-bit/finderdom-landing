"""🔁 Duplikaty / konkurencja — czysta logika dopasowania (bez I/O), wspólna dla API (api/index.py) i jobu wsadowego
(scripts/compute_duplicates.py), który raz dziennie zapisuje przy każdej ofercie gotowe podsumowanie `dup` (kopie, pośrednicy, portale)."""
import math
import re
from datetime import datetime, timezone
from typing import Optional


def _build_diacritic_regex(text: str) -> str:
    """Buduje regex ktory pasuje case+diacritic-insensitive.
    Np. "gdansk" -> "[gG][dD][aąAĄ][nńNŃ][sS][kK]"
    Uzywane dla filtrow po miescie/nazwach z polskimi znakami.
    """
    if not text:
        return ""
    # Mapa: kazdy znak (nawet z ogonkiem) -> zbior alternatyw
    diac_map = {
        'a': 'aąAĄ', 'ą': 'aąAĄ',
        'c': 'cćCĆ', 'ć': 'cćCĆ',
        'e': 'eęEĘ', 'ę': 'eęEĘ',
        'l': 'lłLŁ', 'ł': 'lłLŁ',
        'n': 'nńNŃ', 'ń': 'nńNŃ',
        'o': 'oóOÓ', 'ó': 'oóOÓ',
        's': 'sśSŚ', 'ś': 'sśSŚ',
        'z': 'zźżZŹŻ', 'ź': 'zźżZŹŻ', 'ż': 'zźżZŹŻ',
    }
    out = []
    import re as _re
    for ch in text:
        lower = ch.lower()
        if lower in diac_map:
            out.append('[' + diac_map[lower] + ']')
        elif ch.isalpha():
            # Zwykla litera - obie wielkosci
            out.append('[' + ch.lower() + ch.upper() + ']')
        else:
            # Znak specjalny (spacja, -, cyfra) - escape
            out.append(_re.escape(ch))
    return ''.join(out)


def _num_or_zero(v) -> float:
    try:
        return float(str(v).replace(" ", "").replace(",", ".")) if v not in (None, "") else 0.0
    except Exception:
        return 0.0



_PORTAL_NAMES = {"otodom": "Otodom", "olx": "OLX", "nol": "Nieruchomosci-online", "nieruchomosci-online": "Nieruchomosci-online", "morizon": "Morizon",
                 "gratka": "Gratka", "domiporta": "Domiporta", "adresowo": "Adresowo", "gethome": "Gethome", "odwlasciciela": "OdWłaściciela",
                 "listaprzetargow": "ListaPrzetargow", "apify": "Otodom"}


def _portal_name(key: str) -> str:
    k = str(key or "").lower()
    for n, v in _PORTAL_NAMES.items():
        if n in k:
            return v
    return k.capitalize() or "Portal"


def _portal_of(doc: dict) -> str:
    src = str(doc.get("source") or "").lower()
    url = str(doc.get("url") or "")
    srcs = doc.get("sources") if isinstance(doc.get("sources"), list) else []
    # Rekord scalony z kilku portali (import Apify: Morizon+Gratka+…): portal główny = pierwszy wpis `sources`, nie „apify”
    if srcs and (src in ("", "apify") or not url) and isinstance(srcs[0], dict) and srcs[0].get("portal"):
        return _portal_name(srcs[0]["portal"])
    for k, v in _PORTAL_NAMES.items():
        if k in src or k in url:
            return v
    return src.capitalize() or "Portal"


def _cluster_copies(d: dict, main_portal: str, include_urls: bool) -> list:
    """Ten sam rekord znaleziony na kilku portalach (`sources[]`, np. Morizon + Gratka): każdy dodatkowy portal = osobna kopia ogłoszenia
    (ten sam pośrednik/eksport, więc nie zwiększa liczby pośredników)."""
    out, seen = [], {main_portal}
    for s in (d.get("sources") if isinstance(d.get("sources"), list) else []):
        if not isinstance(s, dict) or not s.get("portal"):
            continue
        p = _portal_name(s["portal"])
        if p in seen:
            continue
        seen.add(p)
        it = {"id": f"{d.get('external_id')}@{p.lower()}", "portal": p, "price": _num_or_zero(s.get("price")) or _num_or_zero(d.get("price")),
              "area_m2": _num_or_zero(d.get("area_m2")), "rooms": d.get("rooms"), "seller_type": d.get("seller_type") or "posrednik",
              "agency": d.get("agency_name") or "", "added_at": d.get("added_at").isoformat() if isinstance(d.get("added_at"), datetime) else d.get("added_at"),
              "is_stale": bool(d.get("is_stale")), "title": (d.get("title") or "")[:90], "image": None, "price_history": [], "same_export": True}
        if include_urls and s.get("url"):
            it["url"] = s["url"]
        out.append(it)
    return out


def _price_timeline(copies: list) -> list:
    """📈 Oś czasu ceny nieruchomości na WSZYSTKICH portalach: z price_history każdej kopii (wpis = stara cena + moment zmiany)
    odtwarzamy: cena startowa od added_at, każda zmiana → nowa cena. Wynik posortowany rosnąco po dacie."""
    ev = []
    for c in copies:
        hist = [h for h in (c.get("price_history") or []) if isinstance(h, dict) and h.get("price")]
        cur = c.get("price") or 0
        start_price = hist[0]["price"] if hist else cur
        if c.get("added_at") and start_price:
            ev.append({"at": c["added_at"], "price": float(start_price), "portal": c["portal"], "agency": c.get("agency") or "", "kind": "start", "cid": c.get("id")})
        for i, h in enumerate(hist):
            new_price = hist[i + 1]["price"] if i + 1 < len(hist) else cur
            if new_price and h.get("at"):
                ev.append({"at": h["at"], "price": float(new_price), "portal": c["portal"], "agency": c.get("agency") or "", "kind": "change",
                           "from": float(h["price"]), "cid": c.get("id")})
    ev.sort(key=lambda e: str(e["at"]))
    return ev[-40:]


_VOIVODESHIPS = {"małopolskie", "mazowieckie", "śląskie", "wielkopolskie", "dolnośląskie", "pomorskie", "łódzkie", "lubelskie", "podkarpackie",
                 "kujawsko-pomorskie", "zachodniopomorskie", "warmińsko-mazurskie", "lubuskie", "opolskie", "świętokrzyskie", "podlaskie"}
_BT_REGEX = {
    "wolnostojacy": r"wolno.?stoj|jednorodzinn|parterow|pi[eę]trow|kanadyjs|szkieletow|dom.z.bali|pasywn",
    "blizniak": r"bli[zź]niak|bli[zź]niacz|dwurodzin|duplex|dwupak",
    "szeregowiec": r"szereg(?:ow|owc|owk)|w szeregu|segment|townhouse",
    "siedliskowy": r"siedlisk|zagrod|gospodarstw",
    "rezydencja": r"rezydencj|\bwilla\b|willow|dw[oó]r(?:ek|ku|em|u|a)|pa[lł]ac|posiad[lł]o[sś][cć]",
    "letniskowy": r"letnisk|rekreac|\brod\b|weekendow",
}
_BT_ALIAS = {"wolnostojacy": ["wolnostojący"], "blizniak": ["bliźniak", "blizniak"], "szeregowiec": ["szeregowy"], "rezydencja": ["willa"]}
_DT_ALIAS = {"budowlana": ["budowlana"], "rolna": ["rolna"], "rekreacyjna": ["rekreacyjna"],
             "lesna": ["lesna", "leśna"], "inwestycyjna": ["inwestycyjna", "usługowa", "uslugowa", "komercyjna"]}
_DT_REGEX = {
    "budowlana": r"budowlan|pod zabudow|pod dom|pod budow",
    "rolna": r"roln[aey]|orn[aey]|uprawn|gospodarstw|ł[aą]k",
    "rekreacyjna": r"rekreac|letnisk|weekend|rod|ogrodow|nad jezior",
    "lesna": r"le[sś]n[aey]|lasu|w lesie|zalesion",
    "inwestycyjna": r"inwestyc|komercyj|us[lł]ugow|przemys[lł]ow|pod rozw[oó]j firm|magazyn|produkcyj",
}

_DUP_STOP = {"mieszkanie", "mieszkania", "sprzedaz", "sprzedaż", "wynajem", "wynajęcia", "wynajecia", "kawalerka", "pokoje", "pokój", "pokoi", "metrów", "metrow",
             "centrum", "blisko", "nowe", "nowa", "super", "okazja", "piękne", "piekne", "przestronne", "komfortowe", "umeblowane", "balkonem", "garażem", "garazem"}
_DUP_STOP_DESC = _DUP_STOP | {"nieruchomość", "nieruchomosci", "nieruchomości", "oferta", "oferty", "sprzedaży", "sprzedazy", "zapraszam", "zapraszamy", "kontakt",
                              "działka", "dzialka", "działce", "dzialce", "budynek", "budynku", "kuchnia", "łazienka", "lazienka", "salon", "sypialnie", "sypialnia",
                              "piętro", "pietro", "parter", "parterze", "garaż", "garaz", "ogród", "ogrod", "ogrodem", "taras", "tarasem", "balkon", "okolica", "okolicy",
                              "cichej", "spokojnej", "zielonej", "idealne", "idealny", "świetna", "swietna", "lokalizacja", "lokalizacji", "komunikacja", "dojazd",
                              "możliwość", "mozliwosc", "również", "rowniez", "został", "zostal", "która", "które", "który", "których", "którym", "bardzo",
                              "wszystkie", "wszystkich", "stanie", "dobrym", "wysokim", "standard", "standardzie", "wykończenia", "wykonczenia", "ogrzewanie", "ogrzewania",
                              "gazowe", "miejsce", "miejsca", "parkingowe", "jednorodzinny", "jednorodzinnych", "wolnostojący", "wolnostojacy", "szeregowy", "bliźniak",
                              "kraków", "krakow", "krakowa", "krakowie", "warszawa", "warszawy", "warszawie", "wrocław", "wroclaw", "wrocławia", "wrocławiu", "poznań", "poznan",
                              "poznania", "poznaniu", "gdańsk", "gdansk", "gdańska", "gdańsku", "łódź", "lodz", "łodzi", "lodzi"}


def _dup_tokens(t) -> set:
    return {w for w in re.findall(r"[a-ząćęłńóśźż]{5,}", str(t or "").lower()) if w not in _DUP_STOP}


def _dup_norm(v) -> str:
    s = str(v or "").strip().lower()
    return "" if s in ("", "none", "null") else s


def _dup_desc_tokens(d: dict) -> set:
    t = (str(d.get("title") or "") + " " + str(d.get("description") or "")).lower()
    words = {w for w in re.findall(r"[a-ząćęłńóśźż]{5,}", t) if w not in _DUP_STOP_DESC}
    nums = {n for n in re.findall(r"\b\d{2,4}(?:[.,]\d{1,2})?\b", t)}  # rok budowy, ary, numer ulicy
    return words | nums


def _dup_ids(d: dict) -> set:
    """Wszystkie identyfikatory rekordu: external_id + numery z url i sources[].url (Morizon mzn…, Gratka /ob/…)."""
    out = set()
    s = re.sub(r"\D", "", str(d.get("external_id") or ""))
    if len(s) >= 6:
        out.add(s)
    for u in [d.get("url")] + [x.get("url") for x in (d.get("sources") or []) if isinstance(x, dict)]:
        for m in re.findall(r"(\d{6,})", str(u or "")):
            out.add(m)
    return out


def _dup_same_street(a: dict, b: dict) -> Optional[bool]:
    sa, sb = _dup_norm(a.get("street")), _dup_norm(b.get("street"))
    if not sa or not sb:
        return None
    sa = re.sub(r"^(ul\.?|al\.?|os\.?|pl\.?)\s*", "", sa); sb = re.sub(r"^(ul\.?|al\.?|os\.?|pl\.?)\s*", "", sb)
    return sa.split()[0][:6] == sb.split()[0][:6]


def _dup_gps_km(a: dict, b: dict) -> Optional[float]:
    try:
        la1, ln1, la2, ln2 = float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"])
    except Exception:
        return None
    return 2 * 6371 * math.asin(math.sqrt(math.sin(math.radians(la2 - la1) / 2) ** 2 + math.cos(math.radians(la1)) * math.cos(math.radians(la2)) * math.sin(math.radians(ln2 - ln1) / 2) ** 2))


def _dup_match(doc: dict, d: dict):
    """Czy `d` to ta sama nieruchomość co `doc`? False = inna; True = prawdopodobnie ta sama;
    "strong" = na pewno ta sama (ten sam identyfikator / ulica / GPS < 300 m / identyczny tytuł / metraż z ułamkiem + cena)."""
    price, area = _num_or_zero(doc.get("price")), _num_or_zero(doc.get("area_m2"))
    is_rental = (doc.get("transaction_type") or "sprzedaz") == "wynajem"
    base_type = str(doc.get("type") or "").lower().split()[0] if doc.get("type") else ""
    d_dist, d_tokens = _dup_norm(doc.get("district")), _dup_tokens(doc.get("title"))
    doc_places = {_dup_norm(doc.get("city")), d_dist} - {""} - _VOIVODESHIPS
    # 1) Ten sam identyfikator (rekord Apify „mzn2048…” = rekord Morizon „2048…”, link do tej samej strony) → na pewno ta sama oferta
    if _dup_ids(doc) & _dup_ids(d):
        return "strong"
    # 2) Twarde sprzeczności: inna ulica, inny rok budowy, GPS dalej niż 1 km, inna liczba pokoi
    if _dup_same_street(doc, d) is False:
        return False
    y1, y2 = _num_or_zero(doc.get("build_year")), _num_or_zero(d.get("build_year"))
    if y1 and y2 and abs(y1 - y2) > 1:
        return False
    km = _dup_gps_km(doc, d)
    if km is not None and km > 1.0:
        return False
    r1, r2 = doc.get("rooms"), d.get("rooms")
    try:
        if r1 not in (None, "", 0) and r2 not in (None, "", 0) and int(float(r1)) != int(float(r2)):
            return False
    except Exception:
        pass
    dd = _dup_norm(d.get("district"))
    d_area = _num_or_zero(d.get("area_m2"))
    # 2b) Inwestycja deweloperska: identyczny metraż, RÓŻNE ceny, a każda cena = metraż × okrągła stawka zł/m² (657 072 = 42,12 × 15 600)
    #     → to różne mieszkania (piętra) w tej samej inwestycji, nie duplikat (Gratka/Morizon rzadko oznaczają rynek pierwotny).
    p2 = _num_or_zero(d.get("price"))
    if area and abs(d_area - area) < 0.05 and abs(p2 - price) >= 1:
        def _dev_price(p):
            ppm = p / area
            return abs(ppm - round(ppm)) < 0.005 and round(ppm) % 50 == 0
        if _dev_price(price) and _dev_price(p2):
            return False
    # 3) Pewne potwierdzenia (ważniejsze niż różnie wpisana dzielnica/gmina): ta sama ulica, GPS < 300 m, identyczny tytuł,
    #    identyczny metraż z ułamkiem (138,84 m²) + identyczna cena
    if _dup_same_street(doc, d) is True or (km is not None and km < 0.3):
        return "strong"
    nt = lambda t: re.sub(r"[^a-ząćęłńóśźż0-9]+", " ", str(t or "").lower()).strip()
    doc_title = nt(doc.get("title"))
    if len(doc_title) >= 15 and doc_title == nt(d.get("title")):
        return "strong"
    if abs(d_area - area) < 0.02 and abs(_num_or_zero(d.get("price")) - price) < 1 and abs(area - round(area)) > 0.01:
        return "strong"
    # 4) Dzielnica/miejscowość: obie znane i różne → inna nieruchomość (chyba że miejscowość jednego = miasto drugiego: gmina vs wieś)
    d_places = {_dup_norm(d.get("city")), dd} - {""} - _VOIVODESHIPS
    if d_dist and dd and d_dist != dd and not (doc_places & d_places):
        return False
    # Rekord bez miejscowości (city = województwo): bez pewnego potwierdzenia łączymy tylko przez zgodne opisy — nigdy „na oko” po cenie i metrażu
    if not doc_places or not d_places:
        if len(str(doc.get("description") or "")) >= 80 and len(str(d.get("description") or "")) >= 80:
            a_t, b_t = _dup_desc_tokens(doc), _dup_desc_tokens(d)
            shared = a_t & b_t
            return len(shared) >= 5 and len(shared) / max(1, min(len(a_t), len(b_t))) >= 0.25
        return False
    if is_rental:
        if abs(d_area - area) > 0.6:
            return False
        if not (d_dist and dd and d_dist == dd) and not (d_tokens & _dup_tokens(d.get("title"))):
            return False
        return True
    # 5) Opisy: ta sama nieruchomość opisywana przez różne biura dzieli nazwy własne / liczby (ulica, ary, rok, cechy).
    #    Dwa różne domy w tym samym mieście o podobnej cenie i metrażu — prawie nic wspólnego.
    if len(str(doc.get("description") or "")) >= 80 and len(str(d.get("description") or "")) >= 80:
        a_t, b_t = _dup_desc_tokens(doc), _dup_desc_tokens(d)
        shared = a_t & b_t
        return len(shared) >= 4 or len(shared) / max(1, min(len(a_t), len(b_t))) >= 0.2
    # 6) Bez opisów: ta sama dzielnica (obie znane) lub wspólne słowo w tytule; dom bez żadnego z tych sygnałów = nie łączymy
    if d_dist and dd and d_dist == dd:
        return True
    if d_tokens & _dup_tokens(d.get("title")):
        return True
    # Mieszkanie, obie strony bez dzielnicy i bez opisów: tylko identyczna cena albo identyczny metraż przy znanej, zgodnej liczbie pokoi
    if base_type not in ("dom", "działka", "dzialka") and not (d_dist or dd):
        rooms_known = r1 not in (None, "", 0) and r2 not in (None, "", 0)
        return abs(_num_or_zero(d.get("price")) - price) < 1 or (rooms_known and abs(d_area - area) < 0.02)
    return False


DUP_PROJ = {"_id": 0, "external_id": 1, "source": 1, "url": 1, "price": 1, "area_m2": 1, "rooms": 1, "seller_type": 1, "agency_name": 1,
            "added_at": 1, "is_stale": 1, "stale_since": 1, "title": 1, "images": 1, "market_type": 1, "district": 1, "price_history": 1, "sources": 1,
            "description": 1, "street": 1, "build_year": 1, "lat": 1, "lng": 1, "city": 1}
DUP_CANDS = 80


def dup_prepare(doc: dict, include_urls: bool):
    """→ (this_copy, extra_this, zapytanie Mongo o kandydatów) — albo zamiast zapytania gotowy wynik (dict z "count"), gdy brak danych do porównania.
    Klucz: to samo miasto + typ + transakcja + cena ±2% + metraż ±3% (+ pokoje gdy oba znane); inna dzielnica = inna nieruchomość;
    wynajem dodatkowo wymaga tej samej dzielnicy lub wspólnego słowa w tytule (okrągłe ceny)."""
    price = _num_or_zero(doc.get("price"))
    area = _num_or_zero(doc.get("area_m2"))
    city = str(doc.get("city") or "").strip()
    this_copy = {"id": str(doc.get("external_id")), "portal": _portal_of(doc), "price": price, "seller_type": doc.get("seller_type") or "posrednik",
                 "agency": doc.get("agency_name") or "", "added_at": doc.get("added_at").isoformat() if isinstance(doc.get("added_at"), datetime) else doc.get("added_at"),
                 "price_history": doc.get("price_history") or []}
    # Rekord scalony z kilku portali (sources[]: Morizon+Gratka itp.) — każdy dodatkowy portal to kopia ogłoszenia
    extra_this = _cluster_copies(doc, this_copy["portal"], include_urls)
    if price < 1000 or area < 5 or not city:
        tl = _price_timeline([this_copy] + extra_this)
        for c in [this_copy] + extra_this:
            c.pop("price_history", None)
        return this_copy, extra_this, {"count": 1 + len(extra_this), "agents": 1 if this_copy["seller_type"] == "posrednik" else 0, "owner_too": this_copy["seller_type"] == "prywatna",
                "agencies": [this_copy["agency"]] if this_copy["agency"].strip() else [], "items": extra_this, "this": this_copy, "timeline": tl, "reason": "brak danych do porównania"}
    base_type = str(doc.get("type") or "").lower().split()[0] if doc.get("type") else ""
    # Lokalizacja: portale różnie wypełniają „miasto” (Otodom: wieś, Morizon/Gratka: powiat, NOL: województwo + miejscowość w dzielnicy).
    # Kandydat pasuje, gdy: to samo miasto, LUB miasto/dzielnica jednego = miasto/dzielnica drugiego, LUB GPS w promieniu ~1 km.
    district = str(doc.get("district") or "").strip()
    place_names = [p for p in {city, district} if p and p.lower() not in _VOIVODESHIPS]
    # Rekordy bez miejscowości (city = województwo, stare importy Otodom/Gratka/NOL) też są kandydatami — o dopasowaniu decyduje _dup_match
    loc_or: list = [{"city": {"$in": sorted(_VOIVODESHIPS)}}]
    for p in place_names:
        rx = {"$regex": "^" + _build_diacritic_regex(p) + "$", "$options": "i"}
        loc_or.append({"city": rx}); loc_or.append({"district": rx})
    try:
        la, ln = float(doc.get("lat")), float(doc.get("lng"))
        if la and ln:
            loc_or.append({"lat": {"$gte": la - 0.01, "$lte": la + 0.01}, "lng": {"$gte": ln - 0.015, "$lte": ln + 0.015}})
    except Exception:
        pass
    q: dict = {
        "external_id": {"$ne": doc.get("external_id")},
        "transaction_type": doc.get("transaction_type") or "sprzedaz",
        "price": {"$gte": price * 0.98, "$lte": price * 1.02},
        "area_m2": {"$gte": area * 0.97, "$lte": area * 1.03},
    }
    and_parts = [{"$or": loc_or}] if place_names or len(loc_or) > 1 else []  # oferta bez miejscowości i GPS: szukamy po całej bazie (decyduje _dup_match: tylko pewne potwierdzenia)
    if base_type:
        q["type"] = {"$regex": "^" + _build_diacritic_regex(base_type), "$options": "i"}
    rooms = doc.get("rooms")
    if rooms not in (None, "", 0):
        rooms_or = [{"rooms": rooms}, {"rooms": {"$in": [None, "", 0]}}]
        try:
            rooms_or.append({"rooms": int(float(rooms))}); rooms_or.append({"rooms": str(int(float(rooms)))})
        except Exception:
            pass
        and_parts.append({"$or": rooms_or})
    if and_parts:
        q["$and"] = and_parts
    return this_copy, extra_this, q


def dup_summarize(doc: dict, this_copy: dict, extra_this: list, cands: list, include_urls: bool) -> dict:
    """Właściwe dopasowanie kandydatów z bazy (cands = wynik zapytania dup_prepare) → {count, agents, owner_too, agencies, this, items, timeline}."""
    items = []
    known_portals = {this_copy["portal"]} | {c["portal"] for c in extra_this}
    matches = {id(d): _dup_match(doc, d) for d in cands}
    # Przechodniość (1 poziom): oferta bez lokalizacji/opisu (np. Otodom z city=województwo) może pasować „na pewno” do znalezionej kopii
    # (identyczny tytuł), choć do oferty wyjściowej już nie — dołączamy ją przez tę kopię.
    strong_found = [d for d in cands if matches[id(d)] == "strong"][:4]
    for d in cands:
        if not matches[id(d)]:
            for f in strong_found:
                if _dup_match(f, d) == "strong":
                    matches[id(d)] = "strong"
                    break
    for d in cands:
        match = matches[id(d)]
        if not match:
            continue
        if match != "strong" and str(d.get("market_type") or "").lower() == "pierwotny" and str(doc.get("market_type") or "").lower() != "pierwotny":
            continue  # inwestycje deweloperskie: wiele identycznych mieszkań ≠ duplikat (chyba że pewne potwierdzenie: id/ulica/GPS/tytuł/metraż)
        same_record = bool(_dup_ids(doc) & _dup_ids(d))  # ten sam rekord zaimportowany drugą ścieżką (Apify „mzn…” vs scraper Morizon) — nie dublujemy portalu
        portal = _portal_of(d)
        if same_record and portal in known_portals:
            continue
        it = {
            "id": str(d.get("external_id")), "portal": portal, "price": _num_or_zero(d.get("price")), "area_m2": _num_or_zero(d.get("area_m2")),
            "rooms": d.get("rooms"), "seller_type": d.get("seller_type") or "posrednik", "agency": d.get("agency_name") or "",
            "added_at": d.get("added_at").isoformat() if isinstance(d.get("added_at"), datetime) else d.get("added_at"),
            "is_stale": bool(d.get("is_stale")), "title": (d.get("title") or "")[:90],
            "image": (d.get("images") or [None])[0] if isinstance(d.get("images"), list) else None,
            "price_history": d.get("price_history") or [],
        }
        if same_record:
            it["same_export"] = True
        if include_urls:
            it["url"] = d.get("url")
        items.append(it)
        if same_record:
            known_portals.add(portal)
        for c in _cluster_copies(d, portal, include_urls):
            if same_record and c["portal"] in known_portals:
                continue
            items.append(c)
            if same_record:
                known_portals.add(c["portal"])
    # Ilu pośredników: pewne minimum — różne nazwy biur, a bez nazw: max kopii na jednym portalu (biura eksportują tę samą ofertę na kilka portali).
    # Kopie z `sources[]` tego samego rekordu (same_export) to ten sam eksport jednego biura — nie liczą się jako kolejny pośrednik.
    items.extend(extra_this)
    # Ten sam portal + ta sama cena raz z `sources[]` (kopia eksportu), raz jako osobny rekord → zostaje osobny rekord (ma własny link i datę).
    # Dwa osobne rekordy na tym samym portalu (dwóch pośredników) zostają oba.
    real_pp = {(it["portal"], round(it["price"] or 0)) for it in items if not it.get("same_export")}
    items = [it for it in items if not (it.get("same_export") and (it["portal"], round(it["price"] or 0)) in real_pp)]
    all_copies = items + [this_copy]
    agents_copies = [c for c in all_copies if c["seller_type"] == "posrednik" and not c.get("same_export")]
    named = {c["agency"].strip().lower() for c in agents_copies if c["agency"].strip()}
    per_portal: dict = {}
    for c in agents_copies:
        if not c["agency"].strip():
            per_portal[c["portal"]] = per_portal.get(c["portal"], 0) + 1
    # Jedno biuro z nazwą (np. z Otodom) + bezimienne kopie po jednej na portal (Gratka, Morizon) = zwykle TEN SAM eksport tego biura →
    # nie dodajemy; sumujemy dopiero, gdy nazwanych biur jest ≥ 2 (wtedy bezimienne kopie to raczej kolejne biura).
    max_pp = max(per_portal.values()) if per_portal else 0
    agents = max(len(named) + (max_pp if len(named) >= 2 else 0), max_pp, 1 if agents_copies else 0)
    timeline = _price_timeline(all_copies)
    for c in all_copies:
        c.pop("price_history", None)
    return {"count": len(items) + 1, "agents": agents, "owner_too": any(c["seller_type"] == "prywatna" for c in all_copies),
            "agencies": sorted({c["agency"] for c in agents_copies if c["agency"].strip()}),
            "this": this_copy, "items": items, "timeline": timeline}
