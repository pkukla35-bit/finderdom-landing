"""Backfill market_type (pierwotny / wtorny) dla ofert bez oznaczenia (mieszkania, domy, lokale — sprzedaż).
Logika = scripts/market_type.infer_market (ta sama co w importerach i POST /api/admin/backfill/market).

Użycie:  python scripts/backfill_market_type.py [--apply] [--type dom] [--since-hours 48]
Domyślnie dry-run (tylko liczy). --since-hours ogranicza do ofert dodanych ostatnio (siatka bezpieczeństwa po imporcie).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(__file__))
from market_type import infer_market  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Zapisz zmiany (domyślnie dry-run)")
    ap.add_argument("--type", default="", help="Ogranicz do typu (lokal/dom/mieszkanie)")
    ap.add_argument("--since-hours", type=int, default=0, help="Tylko oferty dodane w ostatnich N godzinach (0 = cała baza)")
    args = ap.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    q = {"transaction_type": {"$ne": "wynajem"}, "market_type": {"$in": [None, ""]},
         "type": {"$regex": "^" + (args.type or "(mieszkanie|dom|lokal)"), "$options": "i"}}
    if args.since_hours > 0:
        q["added_at"] = {"$gte": (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).isoformat()}
    proj = {"title": 1, "description": 1, "build_year": 1, "seller_type": 1, "type": 1}

    scanned, ops, by_val = 0, [], {}
    for d in db.listings.find(q, proj):
        scanned += 1
        new = infer_market(None, d.get("title") or "", d.get("description") or "", d.get("build_year"), d.get("seller_type"), d.get("type"))
        by_val[new or "puste"] = by_val.get(new or "puste", 0) + 1
        if new:
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": {"market_type": new, "market_scanned": True}}))

    print(f"Przeskanowano: {scanned}, do uzupełnienia: {len(ops)}  {by_val}")
    if args.apply and ops:
        total = 0
        for i in range(0, len(ops), 500):
            r = db.listings.bulk_write(ops[i:i + 500], ordered=False)
            total += r.modified_count
        print(f"Zaktualizowano: {total}")
    elif not args.apply:
        print("Dry-run — użyj --apply, aby zapisać.")


if __name__ == "__main__":
    main()
