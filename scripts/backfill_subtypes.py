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
from market_type import infer_rooms, infer_build_year  # noqa: E402


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
    # 🚪 liczba pokoi z tytułu/opisu (mieszkania i domy bez `rooms`)
    ups, unknown = [], 0
    for d in coll.find({"type": {"$regex": "^(mieszkanie|dom)", "$options": "i"}, "rooms": {"$in": [None, 0, ""]}}, {"title": 1, "description": 1}):
        n = infer_rooms(d.get("title"), d.get("description"))
        if n:
            ups.append(UpdateOne({"_id": d["_id"]}, {"$set": {"rooms": n, "rooms_inferred": True}}))
        else:
            unknown += 1
    if apply:
        for i in range(0, len(ups), 500):
            coll.bulk_write(ups[i:i + 500], ordered=False)
    out["rooms"] = {"updated": len(ups) if apply else 0, "would_update": len(ups), "still_unknown": unknown}
    print("rooms", out["rooms"])
    # 📅 rok budowy z tytułu/opisu (mieszkania, domy, lokale bez `build_year`; portale podają go dla ~1,5% ofert).
    # Raz przeskanowane rekordy dostają year_scanned (opis ~250 MB w bazie — nie czytamy go codziennie od nowa; po zmianie regexów: db.listings.updateMany({}, {$unset: {year_scanned: 1}})).
    q = {"type": {"$regex": "^(mieszkanie|dom|lokal)", "$options": "i"}, "build_year": {"$in": [None, 0, ""]}, "year_scanned": {"$ne": True}}
    ups, scanned_only, by_decade = [], [], {}
    for d in coll.find(q, {"title": 1, "description": 1}):
        y = infer_build_year(d.get("title"), d.get("description"))
        if y:
            by_decade[f"{y // 10 * 10}s"] = by_decade.get(f"{y // 10 * 10}s", 0) + 1
            ups.append(UpdateOne({"_id": d["_id"]}, {"$set": {"build_year": y, "build_year_inferred": True, "year_scanned": True}}))
        else:
            scanned_only.append(d["_id"])
    if apply:
        for i in range(0, len(ups), 500):
            coll.bulk_write(ups[i:i + 500], ordered=False)
        for i in range(0, len(scanned_only), 5000):
            coll.update_many({"_id": {"$in": scanned_only[i:i + 5000]}}, {"$set": {"year_scanned": True}})
    out["build_year"] = {"updated": len(ups) if apply else 0, "would_update": len(ups), "still_unknown": len(scanned_only), "by_decade": dict(sorted(by_decade.items()))}
    print("build_year", out["build_year"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-rescan", action="store_true")
    a = ap.parse_args()
    run(a.apply, not a.no_rescan)
