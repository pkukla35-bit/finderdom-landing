"""Backfill market_type → 'pierwotny' dla istniejących ofert (lokale/domy/mieszkania), które portal
oznaczył jako wtórny / brak, a rok budowy lub treść wskazują rynek pierwotny. Logika = import_apify.infer_market_type.

Użycie:  python scripts/backfill_market_type.py [--apply] [--type lokal]
Domyślnie dry-run (tylko liczy).
"""
import argparse
import os
import sys

from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(__file__))
from import_apify import infer_market_type  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Zapisz zmiany (domyślnie dry-run)")
    ap.add_argument("--type", default="", help="Ogranicz do typu (lokal/dom/mieszkanie)")
    args = ap.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    q = {"transaction_type": "sprzedaz", "market_type": {"$ne": "pierwotny"}}
    if args.type:
        q["type"] = args.type
    proj = {"title": 1, "description": 1, "build_year": 1, "market_type": 1, "type": 1}

    scanned = 0
    ops = []
    by_type = {}
    for d in db.listings.find(q, proj):
        scanned += 1
        new = infer_market_type(d.get("market_type"), d.get("title") or "", d.get("description") or "", d.get("build_year"), d.get("type"))
        if new == "pierwotny":
            by_type[d.get("type")] = by_type.get(d.get("type"), 0) + 1
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": {"market_type": "pierwotny"}}))

    print(f"Przeskanowano: {scanned}, do zmiany na pierwotny: {len(ops)}  {by_type}")
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
