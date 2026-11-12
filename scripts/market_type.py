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
