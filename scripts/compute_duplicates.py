"""🔁 Duplikaty / konkurencja — job wsadowy: dla każdej aktywnej oferty liczy (tym samym algorytmem co strona oferty, scripts/dup_logic.py)
podsumowanie `dup` i zapisuje je przy ofercie, żeby wyniki wyszukiwania (/szukaj) pokazywały kopie, liczbę pośredników i portale
na CAŁEJ bazie — a nie przybliżeniem liczonym w przeglądarce na załadowanej paczce.

  dup: {copies, agents, owner_too, agencies[], portals[], portal_count, price_min, price_max, items[≤12]{id,portal,price,seller_type,agency,same_export}, checked_at, sig}

Kolejność: najpierw oferty bez `dup`, potem te, którym zmieniła się cena/metraż (sig) albo sprawdzone najdawniej. Budżet czasu i wątki
(pymongo jest thread-safe). Użycie: python scripts/compute_duplicates.py [--minutes 240] [--threads 8] [--limit N] [--dry-run]
Wołany też na końcu scripts/compute_ai_verdicts.py (ten sam codzienny workflow).
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dup_logic import dup_prepare, dup_summarize, DUP_PROJ, DUP_CANDS, _num_or_zero  # noqa: E402

SELECT_PROJ = {**DUP_PROJ, "_id": 1, "dup.checked_at": 1, "dup.sig": 1}
RECHECK_DAYS = 5
DUP_VERSION = 4   # podbij po zmianie algorytmu w dup_logic.py → wszystkie oferty zostaną policzone od nowa


def _sig(doc: dict) -> str:
    return f"{round(_num_or_zero(doc.get('price')))}|{round(_num_or_zero(doc.get('area_m2')), 1)}|{doc.get('city') or ''}"


def summarize_doc(coll, doc: dict) -> dict:
    this_copy, extra_this, q = dup_prepare(doc, False)
    if isinstance(q, dict) and "count" in q:
        res = q
    else:
        cands = list(coll.find(q, DUP_PROJ).sort("added_at", DESCENDING).limit(DUP_CANDS))
        res = dup_summarize(doc, this_copy, extra_this, cands, False)
    items = res.get("items") or []
    copies = items + [res.get("this") or {}]
    prices = [c["price"] for c in copies if c.get("price")]
    return {
        "copies": res.get("count", 1), "agents": res.get("agents", 0), "owner_too": bool(res.get("owner_too")),
        "agencies": (res.get("agencies") or [])[:8],
        "portals": sorted({c["portal"] for c in copies if c.get("portal")}),
        "portal_count": len({c["portal"] for c in copies if c.get("portal")}),
        "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
        "items": [{"id": it.get("id"), "portal": it.get("portal"), "price": it.get("price"), "seller_type": it.get("seller_type"),
                   "agency": it.get("agency") or "", **({"same_export": True} if it.get("same_export") else {})} for it in items[:12]],
        "checked_at": datetime.now(timezone.utc).isoformat(), "sig": _sig(doc), "v": DUP_VERSION,
    }


def run(minutes: float = 240, threads: int = 8, limit: int = 0, dry_run: bool = False) -> dict:
    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    coll = db.listings
    for keys in ([("transaction_type", ASCENDING), ("price", ASCENDING)], [("dup.checked_at", ASCENDING)],
                 [("dup.agents", ASCENDING)], [("dup.portal_count", ASCENDING)], [("dup.copies", ASCENDING)], [("dup.v", ASCENDING)]):
        try:
            coll.create_index(keys, background=True)
        except Exception as e:
            print("index:", str(e)[:120])

    deadline = time.time() + minutes * 60
    base = {"is_stale": {"$ne": True}, "price": {"$gte": 1000}, "area_m2": {"$gte": 5}}
    stale_cut = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).isoformat()
    phases = [("nowe (bez dup / stara wersja)", {**base, "dup.v": {"$ne": DUP_VERSION}}, [("added_at", DESCENDING)]),
              ("odświeżenie (> %d dni)" % RECHECK_DAYS, {**base, "dup.checked_at": {"$lt": stale_cut}}, [("dup.checked_at", ASCENDING)])]
    stats = {"scanned": 0, "updated": 0, "multi_agents": 0, "multi_portal": 0, "errors": 0}

    def work(doc):
        try:
            d = summarize_doc(coll, doc)
            if not dry_run:
                coll.update_one({"_id": doc["_id"]}, {"$set": {"dup": d}})
                # 🔄 Kopie policzone ZANIM ta oferta trafiła do bazy nie wiedzą o niej (np. Otodom z 10.11 vs Morizon doimportowany 11.11)
                #    → oznacz do przeliczenia (dup.v=0 ⇒ trafiają do fazy „nowe” w tym samym przebiegu).
                ids = [it["id"] for it in d["items"] if it.get("id")]
                if ids:
                    coll.update_many({"external_id": {"$in": ids}, "dup.checked_at": {"$lt": str(doc.get("added_at") or "")}, "dup.v": {"$gt": 0}},
                                     {"$set": {"dup.v": 0}})
            return d
        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] <= 5:
                print("błąd:", doc.get("external_id"), str(e)[:160])
            return None

    for name, q, sort in phases:
        if time.time() > deadline or (limit and stats["scanned"] >= limit):
            break
        print(f"== faza: {name}")
        last_id = None
        passes = 0
        while time.time() < deadline and not (limit and stats["scanned"] >= limit):
            qq = dict(q)
            if last_id is not None and name.startswith("nowe"):
                qq["_id"] = {"$lt": last_id} if sort[0][1] == DESCENDING else {"$gt": last_id}
            batch_sort = sort if not name.startswith("nowe") else [("_id", DESCENDING)]
            batch = list(coll.find(qq, SELECT_PROJ).sort(batch_sort).limit(400))
            if not batch:
                if name.startswith("nowe") and last_id is not None and passes < 3:
                    passes += 1; last_id = None; continue   # kolejny przebieg: oferty oznaczone dup.v=0 w trakcie (mogły być już „za kursorem”)
                break
            if name.startswith("nowe"):
                last_id = batch[-1]["_id"]
            with ThreadPoolExecutor(max_workers=threads) as ex:
                results = list(ex.map(work, batch))
            for r in results:
                stats["scanned"] += 1
                if r:
                    stats["updated"] += 1
                    if r["agents"] >= 2: stats["multi_agents"] += 1
                    if r["portal_count"] >= 2: stats["multi_portal"] += 1
            if stats["scanned"] % 4000 < 400:
                print(f"  {stats}  ({(deadline - time.time()) / 60:.0f} min zostało)")
            if dry_run and not name.startswith("nowe"):
                break  # dry-run bez zapisu zapętliłby się na tych samych rekordach
    print("KONIEC:", stats)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=240)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.minutes, a.threads, a.limit, a.dry_run)
