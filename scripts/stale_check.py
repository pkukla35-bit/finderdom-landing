"""Rewalidacja starych ofert po imporcie — wykrywa ogłoszenia zdjęte z portalu i oznacza is_stale.

Dlaczego nie wystarczy HEAD + 404/410 (jak w /api/admin/cleanup-stale):
  • Domiporta: zdjęta oferta → 200 ze stroną "Archiwalne" ALBO redirect 302 na listę wyszukiwania,
  • Nieruchomosci-online: 200 + <div class="box-archive-info"> / <input name="isArchive" value="1">,
  • OdWlasciciela: redirect na stronę główną (200),
  • Otodom: 410 (łapane), ale 185 tys. ofert przy 4 tys. sprawdzeń/dobę = nigdy.
Dlatego każdy job importu po zakończeniu sprawdza STALE_SWEEP_LIMIT najdawniej widzianych ofert SWOJEGO portalu
(GET pierwszych ~300 KB, reguły per portal) — razem kilkadziesiąt tysięcy sprawdzeń dziennie, bez nowych workflowów.

Użycie: from stale_check import sweep; sweep(coll, "domiporta", log)
Env: STALE_SWEEP_LIMIT (domyślnie 400), STALE_SWEEP_CONCURRENCY (8), STALE_SWEEP_MIN_AGE_DAYS (3 — nie sprawdzaj ofert
     widzianych w imporcie w ostatnich N dniach), STALE_SWEEP_DRY=1 (tylko loguj).
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from pymongo import UpdateOne

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover
    _creq = None

# Ścieżka strony OFERTY per portal — jeśli po redirectach lądujemy gdzie indziej (lista, strona główna) → oferta zdjęta.
OFFER_PATH = {
    "domiporta": re.compile(r"^/nieruchomosci/[^/?]+/?$"),
    "otodom": re.compile(r"^/pl/oferta/"),
    "gratka": re.compile(r"/ob/\d+"),
    "morizon": re.compile(r"^/oferta/"),
    "nol": re.compile(r"/\d{5,}\.html$"),
    "adresowo": re.compile(r"^/o/"),
    "gethome": re.compile(r"^/(oferta|inwestycja)/"),
    "odwlasciciela": re.compile(r"^/oferty/podglad/"),
    "olx": re.compile(r"^/d/oferta/"),
    "sprzedajemy": re.compile(r"/[a-z0-9-]+-nr\d+"),
    "lento": re.compile(r"/[a-z0-9-]+-\d+\.html$"),
    "allegrolokalnie": re.compile(r"^/oferta/"),
    "krn": re.compile(r"/oferta/|/\d{5,}"),
    "okolica": re.compile(r"/oferta|/\d{4,}"),
    "listaprzetargow": re.compile(r"^/oferty/\d+-"),
}

# Markery w HTML strony oferty (200) oznaczające ofertę zdjętą — tylko jednoznaczne frazy/atrybuty, nie słowa z opisów.
GONE_MARKERS = {
    "nol": [re.compile(r'class="box-archive-info'), re.compile(r'name="isArchive"\s+value="1"')],
    "domiporta": [re.compile(r"<title>[^<]*Archiwaln", re.I), re.compile(r"Oferta archiwalna|Ogłoszenie archiwalne|Ta oferta jest nieaktualna", re.I)],
    "otodom": [re.compile(r'"adStatus"\s*:\s*"(ARCHIVED|INACTIVE|REMOVED)"'), re.compile(r"To ogłoszenie (jest )?nieaktualne|Ogłoszenie nieaktualne|Ogłoszenie zostało zakończone", re.I)],
    "morizon": [re.compile(r"Ta oferta jest (już )?nieaktualna|Oferta nieaktualna|oferta-nieaktualna", re.I)],
    "gratka": [re.compile(r"Ogłoszenie (jest )?nieaktualne|To ogłoszenie zostało zakończone|Ogłoszenie zarchiwizowane", re.I)],
    "adresowo": [re.compile(r"Ogłoszenie nieaktualne|Oferta nieaktualna|Ogłoszenie zostało usunięte", re.I)],
    "gethome": [re.compile(r"Oferta (jest )?nieaktualna|Ogłoszenie nieaktualne", re.I)],
    "sprzedajemy": [re.compile(r"Ogłoszenie (jest )?nieaktualne|Ogłoszenie zakończone|Ogłoszenie zostało zakończone", re.I)],
    "lento": [re.compile(r"Ogłoszenie (wygasło|nieaktualne|zostało zakończone|zostało usunięte)", re.I)],
    "allegrolokalnie": [re.compile(r"Ogłoszenie zakończone|To ogłoszenie zostało zakończone|Ogłoszenie nieaktualne", re.I)],
    "olx": [re.compile(r"Ogłoszenie (jest )?nieaktualne|To ogłoszenie nie jest już dostępne", re.I)],
    "krn": [re.compile(r"Oferta nieaktualna|Ogłoszenie nieaktualne|Oferta archiwalna", re.I)],
    "okolica": [re.compile(r"Oferta nieaktualna|Ogłoszenie nieaktualne|Oferta archiwalna", re.I)],
    "listaprzetargow": [re.compile(r"Przetarg zakończony|Ogłoszenie nieaktualne|Oferta archiwalna", re.I)],
}


# Ile ofert sprawdzać po jednym jobie importu — proporcjonalnie do wielkości portalu w bazie (Otodom 185 tys.).
DEFAULT_LIMIT = {"otodom": 1200, "domiporta": 600, "gratka": 600, "morizon": 600, "adresowo": 500, "gethome": 500, "nol": 500}


def classify(source: str, url: str, status: int, final_url: str, html: str) -> Tuple[str, str]:
    """→ ("gone"|"alive"|"unknown", powód). Konserwatywnie: w razie wątpliwości 'unknown' (nic nie zmieniamy)."""
    if status in (404, 410):
        return "gone", f"http_{status}"
    if status in (403, 429, 503) or status >= 500:
        return "unknown", f"http_{status}"      # blokada / awaria portalu — nie karzemy oferty
    if 400 <= status < 500:
        return "gone", f"http_{status}"
    # redirect poza stronę oferty (lista, strona główna)
    try:
        p0, p1 = urlparse(url), urlparse(final_url or url)
        if p1.path in ("", "/") and p0.path not in ("", "/"):
            return "gone", "redirect_home"
        pat = OFFER_PATH.get(source)
        if pat and pat.search(p0.path) and not pat.search(p1.path):
            return "gone", "redirect_list"
        if source == "domiporta" and p1.query and "Localizations" in p1.query:
            return "gone", "redirect_list"
    except Exception:
        pass
    if html:
        for rx in GONE_MARKERS.get(source, []):
            if rx.search(html):
                return "gone", f"marker:{rx.pattern[:30]}"
        return "alive", "ok"
    return "unknown", "no_html"


def _fetch(url: str, timeout: float = 20.0) -> Tuple[int, str, str]:
    """GET (do ~300 KB) przez curl_cffi z odciskiem Chrome. → (status, final_url, html)."""
    if _creq is None:
        raise RuntimeError("curl_cffi not installed")
    r = _creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True,
                  headers={"Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,*/*;q=0.8"})
    body = r.text or ""
    return r.status_code, str(r.url or url), body[:300_000]


def sweep(coll, source: str, log, limit: Optional[int] = None, concurrency: Optional[int] = None,
          min_age_days: Optional[int] = None, dry: Optional[bool] = None) -> Dict[str, int]:
    """Sprawdza `limit` najdawniej widzianych (last_seen_at) aktywnych ofert portalu `source`
    i oznacza zdjęte jako is_stale (stale_reason="page_gone:<powód>"). Zwraca liczniki."""
    if limit is None:
        limit = os.environ.get(f"STALE_SWEEP_LIMIT_{source.upper()}") or os.environ.get("STALE_SWEEP_LIMIT") or DEFAULT_LIMIT.get(source, 400)
    limit = int(limit)
    concurrency = int(concurrency if concurrency is not None else os.environ.get("STALE_SWEEP_CONCURRENCY", "8"))
    min_age_days = int(min_age_days if min_age_days is not None else os.environ.get("STALE_SWEEP_MIN_AGE_DAYS", "3"))
    dry = bool(dry if dry is not None else os.environ.get("STALE_SWEEP_DRY") == "1")
    if limit <= 0:
        return {"checked": 0, "gone": 0, "alive": 0, "unknown": 0}
    now = datetime.now(timezone.utc)
    seen_cut = (now - timedelta(days=min_age_days)).isoformat()      # added_at/last_seen_at są ISO stringami
    chk_cut = now - timedelta(days=2)
    q = {"source": source, "is_stale": {"$ne": True}, "url": {"$regex": "^https?://"},
         "last_seen_at": {"$lt": seen_cut},
         "$or": [{"stale_checked_at": {"$exists": False}}, {"stale_checked_at": {"$lt": chk_cut}}]}
    docs = list(coll.find(q, {"_id": 1, "url": 1}).sort("last_seen_at", 1).limit(limit))
    if not docs:
        log.info("stale-sweep %s: nic do sprawdzenia (wszystkie oferty widziane < %dd lub sprawdzone < 2d)", source, min_age_days)
        return {"checked": 0, "gone": 0, "alive": 0, "unknown": 0}
    t0 = time.time()
    results = []

    def work(d):
        try:
            st, fin, html = _fetch(d["url"])
            kind, why = classify(source, d["url"], st, fin, html)
        except Exception as e:
            kind, why, st = "unknown", f"err:{type(e).__name__}", 0
        return d["_id"], kind, why, st

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for r in ex.map(work, docs):
            results.append(r)
    ops = []
    cnt = {"checked": len(results), "gone": 0, "alive": 0, "unknown": 0}
    reasons: Dict[str, int] = {}
    for _id, kind, why, st in results:
        cnt[kind] += 1
        if kind == "gone":
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
            ops.append(UpdateOne({"_id": _id}, {"$set": {"is_stale": True, "stale_since": now, "stale_reason": f"page_gone:{why[:40]}",
                                                           "stale_checked_at": now, "last_checked_at": now}}))
        else:
            ops.append(UpdateOne({"_id": _id}, {"$set": {"stale_checked_at": now, **({"last_checked_at": now} if kind == "alive" else {})}}))
    if ops and not dry:
        coll.bulk_write(ops, ordered=False)
    log.info("stale-sweep %s: checked=%d gone=%d alive=%d unknown=%d reasons=%s in %.0fs%s",
             source, cnt["checked"], cnt["gone"], cnt["alive"], cnt["unknown"], reasons, time.time() - t0, " (DRY)" if dry else "")
    return cnt
