"""Rynek pierwotny / wtórny — wspólna klasyfikacja dla importerów bezpośrednich (Otodom, OLX, NOL, Gratka/Morizon/...)
i backfillu. Ta sama logika co POST /api/admin/backfill/market, żeby nowe oferty dostawały market_type od razu przy imporcie
(wcześniej: nowe domy/mieszkania wchodziły z pustym market_type → filtr „pierwotny” nie pokazywał nic z dzisiejszych importów).
"""
import re
from datetime import datetime, timezone

PIERWOTNY_RX = re.compile(
    r"dewelopers?k|od\s+dewelopera|rynek\s+pierwotn|rynku\s+pierwotn|stan\s+deweloper|nowa\s+inwestycj|now(?:ej|ą)\s+inwestycj|"
    r"oddani[ea]\s+(?:w\s+)?(?:20\d\d|[ivx]+\s*kw)|termin\s+oddania|prognoza\s+oddania|odbi[oó]r\s+kluczy|budowa\s+w\s+toku|w\s+budowie|"
    r"nowobudowan|stan\s+surow|przedsprzeda[żz]|bez\s+(?:pcc|podatku\s+pcc)|0\s*%\s*pcc|bez\s+prowizji\s+i\s+pcc", re.I)
WTORNY_RX = re.compile(
    r"rynek\s+wt[oó]rn|rynku\s+wt[oó]rn|\bwt[oó]rn|do\s+remontu|po\s+remoncie|po\s+generalnym|kamienic|wielk(?:a|iej)\s+p[łl]yt|"
    r"z\s+lat\s+(?:[5-9]0|dwutysięcznych)|peerel|przedwojenn|blok\s+z\s+lat", re.I)


def infer_market(portal_market, title, description, build_year=None, seller_type=None, ptype=None):
    """→ 'pierwotny' | 'wtorny' | None. Jawne oznaczenie portalu ma pierwszeństwo; działki/garaże nie mają rynku."""
    pm = (portal_market or "").lower().replace("ó", "o")
    if pm in ("pierwotny", "primary", "primary_market"):
        return "pierwotny"
    if pm in ("wtorny", "secondary", "secondary_market"):
        return "wtorny"
    if ptype and not re.match(r"^(mieszkanie|dom|lokal)", str(ptype), re.I):
        return None
    text = f"{title or ''} {(description or '')[:1500]}"
    if seller_type == "deweloper" or PIERWOTNY_RX.search(text):
        return "pierwotny"
    if WTORNY_RX.search(text):
        return "wtorny"
    try:
        y = int(float(build_year or 0))
    except (TypeError, ValueError):
        y = 0
    year_now = datetime.now(timezone.utc).year
    if y >= year_now - 1:
        return "pierwotny"
    if 1800 < y <= year_now - 4:
        return "wtorny"
    return None


_ROOM_WORDS = {"jedno": 1, "dwu": 2, "trzy": 3, "cztero": 4, "pięcio": 5, "piecio": 5, "sześcio": 6, "szescio": 6}


def infer_rooms(title, description=None):
    """Liczba pokoi z tytułu/opisu, gdy portal jej nie podał (Szczecin 11.11: 27 z 72 nowych mieszkań bez pokoi → nie wchodziły
    w żaden chip pokoi). „kawalerka”=1, „3 pokoje / 3-pok. / 3 pokojowe / trzypokojowe / M3”=3. → int | None"""
    text = f"{title or ''} {(description or '')[:600]}".lower()
    if re.search(r"\bkawalerk|\bgarsonier", text):
        return 1
    m = re.search(r"\b(\d{1,2})\s*[- ]?\s*(?:pok(?:\.|oj|ój|oje|oi|ojow)|pokojow)", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    for w, n in _ROOM_WORDS.items():
        if re.search(rf"\b{w}\s*-?\s*pokoj", text):
            return n
    m = re.search(r"\bm-?([1-6])\b", text)
    if m and "mieszkan" in text:
        return int(m.group(1))
    return None


_YR = r"(1[89]\d\d|20\d\d)"
_DECADE_WORDS = {"dwudziestych": 1925, "trzydziestych": 1935, "czterdziestych": 1945, "pięćdziesiątych": 1955, "piecdziesiatych": 1955,
                 "sześćdziesiątych": 1965, "szescdziesiatych": 1965, "siedemdziesiątych": 1975, "siedemdziesiatych": 1975,
                 "osiemdziesiątych": 1985, "osiemdziesiatych": 1985, "dziewięćdziesiątych": 1995, "dziewiecdziesiatych": 1995,
                 "dwutysięcznych": 2005, "dwutysiecznych": 2005}
_BUILD_YEAR_RX = [
    # „rok budowy: 1985”, „rok bud. 2009”, „r.b. 1978”, „rocznik 1992”
    re.compile(r"(?:rok\s+bud(?:owy|\.|\b)|rocznik|\br\.?\s?b\.?)\s*[:\-–.]?\s*(?:ok\.?\s*)?" + _YR),
    # „wybudowany w 2008”, „oddany do użytku w 2019 roku”, „zbudowany 1935”
    re.compile(r"(?:wybudowan|zbudowan|postawion|wzniesion|oddan)\w*\s+(?:do\s+u[żz]ytk\w+\s+)?(?:w\s+)?(?:roku\s+)?" + _YR),
    # „budynku z 1976”, „kamienica z 1910 roku”, „blok z 1985 r.”, „w budynku z roku 2000”
    re.compile(r"(?:budyn\w+|kamienic\w+|blok\w*|dom\w*|wie[żz]ow\w+|apartamentow\w+)\s+z\s+(?:roku\s+)?" + _YR),
    # „z 1985 roku” / „z 1985r.” — rok z dopiskiem, żeby nie łapać „z 2000 zł”
    re.compile(r"\bz\s+" + _YR + r"\s*(?:r\b|r\.|roku)"),
]
# Inwestycje deweloperskie: „oddanie w 2029”, „termin oddania: IV kw. 2026”, „odbiór kluczy 2027” — rok planowany (tylko bieżący−1 … +5)
_PLANNED_RX = re.compile(r"(?:oddani[ea]|termin\s+oddania|odbi[oó]r\w*|zako[ńn]czeni[ea]\s+budowy|planowan\w+\s+(?:oddanie|zako[ńn]czenie))[^\d]{0,30}?(20\d\d)")
_DECADE_RX = re.compile(r"\bz\s+lat\s+(?:(\d0)\.?(?:\s*-?\s*(?:tych|ych))?\b|(" + "|".join(_DECADE_WORDS) + r"))")


def infer_build_year(title, description=None):
    """Rok budowy z tytułu/opisu, gdy portal go nie podał (listy Gratka/Morizon/OLX/Adresowo nie mają pola → 6 z 400 mieszkań
    w Szczecinie miało rok → filtr „Rok budowy” zwracał 0). Tylko jawne zapisy; „z lat 70.” → 1975. → int | None"""
    text = f"{title or ''} {(description or '')[:2000]}".lower().replace("\xa0", " ")
    year_now = datetime.now(timezone.utc).year
    for rx in _BUILD_YEAR_RX:
        m = rx.search(text)
        if m:
            y = int(m.group(1))
            if 1800 <= y <= year_now + 5:
                return y
    m = _PLANNED_RX.search(text)
    if m:
        y = int(m.group(1))
        if year_now - 1 <= y <= year_now + 5:
            return y
    m = _DECADE_RX.search(text)
    if m:
        if m.group(2):
            return _DECADE_WORDS[m.group(2)]
        dd = int(m.group(1))
        if 20 <= dd <= 90:
            return 1900 + dd + 5
    return None
