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
    "homeportal": re.compile(r"/[A-Z0-9]+-[A-Z]{2}-\d+/?$"),
    "domy": re.compile(r"/dol\d+$"),
    "klikmapa": re.compile(r"^/oferty/[a-z0-9-]+"),
    "szybko": re.compile(r"/oferta-\d+$"),
    "domplace": re.compile(r"^/ogloszenie/[a-z0-9-]+-\d+$"),
    "mojelokum": re.compile(r",\d+,0,0,0,0,0,0,0,0,0,0,ogloszenie,ogloszenia\.html$"),   # 410 = zdjęte
}

# Markery w HTML strony oferty (200) oznaczające ofertę zdjętą — tylko jednoznaczne frazy/atrybuty, nie słowa z opisów.
GONE_MARKERS = {
    "nol": [re.compile(r'class="box-archive-info'), re.compile(r'name="isArchive"\s+value="1"')],
    # Domiporta (zweryfikowane 20.11): <title>… - Archiwum Domiporta.pl</title>, <h3 class="archive__title">To ogłoszenie jest już nieaktualne.</h3>
    "domiporta": [re.compile(r"<title>[^<]*Archiwum Domiporta", re.I), re.compile(r'class="archive__(title|content|info)"'),
                  re.compile(r"To ogłoszenie jest już nieaktualne|Oferta archiwalna|Ogłoszenie archiwalne", re.I)],
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
    # homeportal/domy/klikmapa/szybko (27.11): brak zweryfikowanych markerów — szybko.pl ma „Oferta nieaktualna” w formularzu zgłoszenia
    # na KAŻDEJ stronie, więc decydują tylko 404/410 i redirect poza stronę oferty.
    "klikmapa": [re.compile(r'"isExpired"\s*:\s*true|&quot;isExpired&quot;:true')],
}


# Ile ofert sprawdzać po jednym jobie importu — proporcjonalnie do wielkości portalu w bazie (Otodom 185 tys.).
# Otodom (Cloudflare) blokuje 403 po ~150–200 szybkich żądaniach z jednego IP → 1 wątek, 0.6 s przerwy, 600/job (~10 min), stop po 15× 403 z rzędu.
DEFAULT_CONCURRENCY = {"otodom": 1}
DEFAULT_DELAY = {"otodom": 0.6}
DEFAULT_LIMIT = {"otodom": 600, "domiporta": 600, "gratka": 600, "morizon": 600, "adresowo": 500, "gethome": 500, "nol": 500}
BLOCK_STREAK_ABORT = 15


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


_SESSION = None


def _fetch(url: str, timeout: float = 20.0) -> Tuple[int, str, str]:
    """GET (do ~300 KB) przez curl_cffi z odciskiem Chrome (wspólna sesja → cookies). → (status, final_url, html)."""
    global _SESSION
    if _creq is None:
        raise RuntimeError("curl_cffi not installed")
    if _SESSION is None:
        _SESSION = _creq.Session(impersonate="chrome")
    r = _SESSION.get(url, timeout=timeout, allow_redirects=True,
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
    if concurrency is None:
        concurrency = os.environ.get(f"STALE_SWEEP_CONCURRENCY_{source.upper()}") or os.environ.get("STALE_SWEEP_CONCURRENCY") or DEFAULT_CONCURRENCY.get(source, 8)
    concurrency = int(concurrency)
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
    if docs and not dry:
        # „claim” — równoległe joby tego samego portalu (matrix) nie sprawdzają tych samych ofert
        coll.update_many({"_id": {"$in": [d["_id"] for d in docs]}}, {"$set": {"stale_checked_at": now}})
    if not docs:
        log.info("stale-sweep %s: nic do sprawdzenia (wszystkie oferty widziane < %dd lub sprawdzone < 2d)", source, min_age_days)
        return {"checked": 0, "gone": 0, "alive": 0, "unknown": 0}
    t0 = time.time()
    results = []
    delay = float(os.environ.get(f"STALE_SWEEP_DELAY_{source.upper()}") or os.environ.get("STALE_SWEEP_DELAY") or DEFAULT_DELAY.get(source, 0.0))
    blocked = {"streak": 0, "abort": False}

    def work(d):
        if blocked["abort"]:
            return d["_id"], "unknown", "skipped_blocked", 0
        try:
            st, fin, html = _fetch(d["url"])
            kind, why = classify(source, d["url"], st, fin, html)
        except Exception as e:
            kind, why, st = "unknown", f"err:{type(e).__name__}", 0
        if st in (403, 429):
            blocked["streak"] += 1
            if blocked["streak"] >= BLOCK_STREAK_ABORT:
                blocked["abort"] = True
        else:
            blocked["streak"] = 0
        if delay:
            time.sleep(delay)
        return d["_id"], kind, why, st

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for r in ex.map(work, docs):
            results.append(r)
    if blocked["abort"]:
        log.warning("stale-sweep %s: portal blokuje (%d× 403/429 z rzędu) — przerwano, reszta wróci do kolejki", source, BLOCK_STREAK_ABORT)
    ops = []
    cnt = {"checked": len(results), "gone": 0, "alive": 0, "unknown": 0}
    reasons: Dict[str, int] = {}
    unknown: Dict[str, int] = {}
    for _id, kind, why, st in results:
        cnt[kind] += 1
        if kind == "unknown":
            unknown[why[:24]] = unknown.get(why[:24], 0) + 1
        if kind == "gone":
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
            ops.append(UpdateOne({"_id": _id}, {"$set": {"is_stale": True, "stale_since": now, "stale_reason": f"page_gone:{why[:40]}",
                                                           "stale_checked_at": now, "last_checked_at": now}}))
        elif kind == "alive":
            ops.append(UpdateOne({"_id": _id}, {"$set": {"stale_checked_at": now, "last_checked_at": now}}))
        else:  # unknown (403/timeout) — zdejmij „claim”, żeby następny job sprawdził ponownie
            ops.append(UpdateOne({"_id": _id}, {"$unset": {"stale_checked_at": ""}}))
    if ops and not dry:
        coll.bulk_write(ops, ordered=False)
    log.info("stale-sweep %s: checked=%d gone=%d alive=%d unknown=%d reasons=%s unknown_why=%s in %.0fs%s",
             source, cnt["checked"], cnt["gone"], cnt["alive"], cnt["unknown"], reasons, unknown, time.time() - t0, " (DRY)" if dry else "")
    return cnt
