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
