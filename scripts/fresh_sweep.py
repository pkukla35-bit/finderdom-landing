"""
Odświeżenie ofert co 30 minut (FRESH SWEEP) — bez zmian w plikach workflow.

Jak to działa:
  • Każdy workflow importu przyjmuje input `max_pages`. Uruchomienie z max_pages=1 to lekki przebieg:
    tylko 1. strona list „od najnowszych” — czyli dokładnie to, co pojawiło się w ostatnich 30 minutach.
  • Job Otodom (mieszkanie/sprzedaz) po skończeniu czeka do najbliższej pełnej/wpół godziny (:00 / :30) w oknie 7:00–21:00 PL
    i odpala kolejną turę (workflow_dispatch przez GitHub API) dla WSZYSTKICH 4 importów → samonapędzająca się pętla.
    Po 21:00 pętla gaśnie; rano wznawia ją poranny pełny import lub backstop w backendzie (26.11: co 30 min, było co godzinę 9–19).
  • Token GitHub bierzemy z Mongo (settings._id="github" → token; zapisuje go admin przez /api/admin/github/token).
  • Backstop: backend (tick z ruchu co 10 min / cleanup-stale co 6 h) wznawia pętlę, jeśli ostatni dispatch był dawniej niż 45 min (settings._id="fresh_sweep").
"""
import json, logging, os, time, urllib.request
from datetime import datetime, timezone

log = logging.getLogger("fresh")
REPO = os.environ.get("GITHUB_REPOSITORY") or "pkukla35-bit/finderdom-landing"
FRESH_INPUTS = {
    "scrape-otodom-regions.yml": {"max_pages": "1", "regions": "1"},
    "scrape-olx.yml":            {"max_pages": "2", "regions": "1"},
    "scrape-nol.yml":            {"max_pages": "2", "max_details": "150", "regions": "1"},
    "scrape-portals.yml":        {"max_pages": "1", "regions": "1"},
}
MAX_WAIT_S = 70 * 60     # nie czekaj dłużej niż 70 min (timeout jobu 120 min)
WINDOW_PL = (7, 21)      # tury co 30 min (:00 i :30) 7:00–21:00 czasu polskiego (ustalenie użytkownika 26.11; wcześniej co godzinę 9–19)
TOUR_EVERY_MIN = 30


def _gh(tok: str, path: str, method: str = "GET", body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "finderdom-fresh"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read() or b"{}")


def github_token(db) -> str:
    doc = db.settings.find_one({"_id": "github"}) or {}
    return (doc.get("token") or os.environ.get("GH_DISPATCH_TOKEN") or "").strip()


def is_fresh_mode(max_pages: int) -> bool:
    return max_pages <= 2 or os.environ.get("FRESH_SWEEP") == "1"


def dispatch_all(db, reason: str) -> dict:
    """Odpala lekką turę wszystkich importów. Pomija workflow, który właśnie biegnie (np. pełny poranny import)."""
    tok = github_token(db)
    if not tok:
        log.warning("fresh: brak tokena GitHub (settings.github.token) — pętla nie ruszy"); return {"error": "no_token"}
    out = {}
    from zoneinfo import ZoneInfo
    hour_pl = datetime.now(ZoneInfo("Europe/Warsaw")).hour
    targets = dict(FRESH_INPUTS)
    # werdykty AI (okazja / norma / zawyżona) dla ofert z ostatnich godzin: przeliczenie całej bazy trwa ~3 min,
    # odpalane w turach 12:00, 16:00 i 19:00 PL (poranne liczy cron 05:30 UTC) — bez edycji pliku workflow (brak uprawnienia „workflow”)
    if hour_pl in (12, 16, 19):
        targets["compute-ai-verdicts.yml"] = {"dry_run": "no"}
    for wf, inputs in targets.items():
        try:
            _, runs = _gh(tok, f"/actions/workflows/{wf}/runs?per_page=5")
            if any(r["status"] in ("queued", "in_progress") for r in runs.get("workflow_runs", [])):
                out[wf] = "running-skip"; continue
            st, _ = _gh(tok, f"/actions/workflows/{wf}/dispatches", "POST", {"ref": "main", "inputs": inputs})
            out[wf] = st
        except Exception as e:
            out[wf] = str(e)[:80]
    db.settings.update_one({"_id": "fresh_sweep"}, {"$set": {"last_dispatch_at": datetime.now(timezone.utc).isoformat(), "last_results": out, "reason": reason}}, upsert=True)
    log.info("fresh: dispatch (%s) → %s", reason, out)
    return out


def next_tour_pl(now=None):
    """Najbliższa granica :00 / :30 w oknie 7–21 PL (datetime aware); gdy dziś już po oknie → jutro 7:00."""
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    pl = ZoneInfo("Europe/Warsaw")
    now = now or datetime.now(pl)
    base = now.replace(second=0, microsecond=0)
    nxt = base + timedelta(minutes=TOUR_EVERY_MIN - (base.minute % TOUR_EVERY_MIN))
    if nxt.hour < WINDOW_PL[0]:
        nxt = nxt.replace(hour=WINDOW_PL[0], minute=0)
    if nxt.hour > WINDOW_PL[1] or (nxt.hour == WINDOW_PL[1] and nxt.minute > 0):
        nxt = (nxt + timedelta(days=1)).replace(hour=WINDOW_PL[0], minute=0)
    return nxt


_LOOP_DONE = False


def claim_tour(db, nxt_iso: str, who: str) -> bool:
    """Atomowo rezerwuje turę `nxt_iso` dla tego jobu (settings.fresh_sweep.claimed_tour). Pierwszy kończący się lekki job
    (OLX, portale, Otodom…) zostaje liderem tury; pozostałe kończą od razu. Dzięki temu pętla nie umiera, gdy Otodom
    jeszcze biegnie i tura dostaje dla niego „running-skip”. 27.11"""
    try:
        r = db.settings.update_one({"_id": "fresh_sweep", "claimed_tour": {"$ne": nxt_iso}},
                                   {"$set": {"claimed_tour": nxt_iso, "claimed_by": who, "claimed_at": datetime.now(timezone.utc).isoformat()}})
        if r.matched_count:
            return True
        if not db.settings.find_one({"_id": "fresh_sweep"}, {"_id": 1}):
            db.settings.update_one({"_id": "fresh_sweep"}, {"$set": {"claimed_tour": nxt_iso, "claimed_by": who}}, upsert=True)
            return True
        return False
    except Exception as e:
        log.warning("fresh: claim failed (%s) — nie czekam", str(e)[:80]); return False


def wait_and_dispatch_next(db, job_started_at: float = None, who: str = "otodom") -> None:
    """Koniec lekkiego jobu: jeśli następna granica :00/:30 mieści się w oknie 7–21 PL i da się na nią poczekać (≤70 min),
    pierwszy job, który ją zarezerwuje (claim_tour), czeka i odpala kolejną turę. Po 21:00 pętla gaśnie; rano wznawia ją
    poranny pełny import albo backstop w backendzie. Wywołanie jest jednorazowe na proces (łańcuch main() w jednym jobie)."""
    global _LOOP_DONE
    if os.environ.get("FRESH_NO_LOOP") == "1" or _LOOP_DONE:
        return
    _LOOP_DONE = True
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    nxt = next_tour_pl(now)
    wait = (nxt - now).total_seconds()
    # job ma timeout 120 min — nie czekaj, jeśli przekroczylibyśmy ~110 min od startu (pełny poranny import trwa długo)
    budget = (110 * 60 - (time.time() - job_started_at)) if job_started_at else MAX_WAIT_S
    if wait > min(MAX_WAIT_S, budget):
        log.info("fresh: następna tura %s PL — za daleko (%.0f min), kończę bez pętli", nxt.strftime("%H:%M"), wait / 60); return
    if not claim_tour(db, nxt.isoformat(), who):
        log.info("fresh: turę %s PL prowadzi już inny job — kończę", nxt.strftime("%H:%M")); return
    log.info("fresh: [%s] czekam %d s do tury %s PL", who, int(wait), nxt.strftime("%H:%M"))
    time.sleep(max(0, wait))
    dispatch_all(db, f"loop-30min {nxt.strftime('%H:%M')} ({who})")
