"""Backfill typu domu (building_type) i przeznaczenia działki (dzialka_type) z tytułu + PEŁNEGO opisu — tam, gdzie portal ich nie podał.
Wcześniejszy backfill (API) skanował tylko 800 znaków opisu i w pierwszej kolejności „wolnostojący” → połowa domów bez typu.
Użycie: python scripts/backfill_subtypes.py [--apply] [--rescan]   (wołany też z backfill_market_type.py — ten sam workflow)."""
import argparse
import os
import re
import sys

from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dup_logic import bt_classify, _DT_REGEX  # noqa: E402


def dt_classify(title, description=None):
    text = f"{title or ''} {description or ''}"
    return next((k for k, pat in _DT_REGEX.items() if re.search(pat, text, re.I)), None)


def run(apply: bool = False, rescan: bool = True) -> dict:
    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    coll = db.listings
    out = {}
    for kind, field, fn in (("dom", "building_type", bt_classify), ("dzialka", "dzialka_type", dt_classify)):
        q = {"type": {"$regex": "^" + kind, "$options": "i"}, field: {"$in": [None, ""]}}
        if not rescan:
            q["subtype_scanned"] = {"$ne": True}
        ups, unknown, by_val = [], 0, {}
        for d in coll.find(q, {"title": 1, "description": 1}):
            val = fn(d.get("title"), d.get("description"))
            by_val[val or "puste"] = by_val.get(val or "puste", 0) + 1
            if val:
                ups.append(UpdateOne({"_id": d["_id"]}, {"$set": {field: val, "subtype_scanned": True}}))
            else:
                unknown += 1
        if apply:
            for i in range(0, len(ups), 500):
                coll.bulk_write(ups[i:i + 500], ordered=False)
        out[kind] = {"updated": len(ups) if apply else 0, "would_update": len(ups), "still_unknown": unknown, "by_value": by_val}
        print(kind, out[kind])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-rescan", action="store_true")
    a = ap.parse_args()
    run(a.apply, not a.no_rescan)
