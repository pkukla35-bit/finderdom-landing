"""Raport rozmiaru bazy MongoDB Atlas (bez danych wrażliwych) — uruchamiany z GitHub Actions.

Opcjonalnie: PRUNE=1 kasuje trwale oferty is_stale starsze niż PRUNE_DAYS (domyślnie 14)
oraz oferty bez zdjęć i bez URL (śmieci importowe).
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
if not uri:
    print("Brak MONGODB_URI"); sys.exit(1)
db = MongoClient(uri)[os.environ.get("MONGODB_DB", "finderdom")]

MB = 1024 * 1024
st = db.command("dbStats")
print(f"== dbStats: dataSize={st['dataSize']/MB:.1f} MB  storageSize={st['storageSize']/MB:.1f} MB  "
      f"indexSize={st['indexSize']/MB:.1f} MB  logical(data+index)={(st['dataSize']+st['indexSize'])/MB:.1f} MB  "
      f"objects={st['objects']}")

print("\n== Kolekcje (dataSize / storage / index / count):")
rows = []
for name in db.list_collection_names():
    c = db.command("collStats", name)
    rows.append((c.get("size", 0), c.get("storageSize", 0), c.get("totalIndexSize", 0), c.get("count", 0), name))
for size, stor, idx, cnt, name in sorted(rows, reverse=True):
    print(f"  {name:32s} {size/MB:8.1f} MB  {stor/MB:8.1f} MB  {idx/MB:7.1f} MB  {cnt:9d}")

L = db.listings
now = datetime.now(timezone.utc)
print("\n== listings — rozkład:")
print("  total:", L.estimated_document_count())
print("  is_stale=true:", L.count_documents({"is_stale": True}))
for d in (7, 14, 30):
    cutoff = now - timedelta(days=d)
    print(f"  is_stale & stale_since > {d} dni:", L.count_documents({"is_stale": True, "stale_since": {"$lt": cutoff}}))
print("  bez zdjęć (images puste/brak):", L.count_documents({"$or": [{"images": {"$exists": False}}, {"images": []}, {"images": None}]}))
print("  bez url:", L.count_documents({"$or": [{"url": {"$exists": False}}, {"url": ""}, {"url": None}]}))
for d in (30, 60, 90):
    iso = (now - timedelta(days=d)).isoformat()
    print(f"  added_at starsze niż {d} dni:", L.count_documents({"added_at": {"$lt": iso}}))
print("  wg source:", {r["_id"]: r["n"] for r in L.aggregate([{"$group": {"_id": "$source", "n": {"$sum": 1}}}])})
print("  wg transaction_type:", {r["_id"]: r["n"] for r in L.aggregate([{"$group": {"_id": "$transaction_type", "n": {"$sum": 1}}}])})

# Średni rozmiar dokumentu + największe pola
sample = list(L.aggregate([{"$sample": {"size": 200}}]))
if sample:
    import bson
    sizes = {}
    for doc in sample:
        for k, v in doc.items():
            sizes[k] = sizes.get(k, 0) + len(bson.BSON.encode({k: v}))
    tot = sum(sizes.values())
    print(f"  śr. rozmiar dokumentu: {tot/len(sample)/1024:.1f} KB; największe pola (udział %):")
    for k, v in sorted(sizes.items(), key=lambda x: -x[1])[:8]:
        print(f"    {k:22s} {100*v/tot:5.1f}%")

if os.environ.get("PRUNE") == "1":
    days = int(os.environ.get("PRUNE_DAYS", "14"))
    cutoff = now - timedelta(days=days)
    r1 = L.delete_many({"is_stale": True, "stale_since": {"$lt": cutoff}})
    r2 = L.delete_many({"$and": [
        {"$or": [{"url": {"$exists": False}}, {"url": ""}, {"url": None}]},
        {"$or": [{"images": {"$exists": False}}, {"images": []}, {"images": None}]},
    ]})
    print(f"\n== PRUNE: usunięto stale>{days}d: {r1.deleted_count}, śmieci bez url i zdjęć: {r2.deleted_count}")
    st2 = db.command("dbStats")
    print(f"   logical po: {(st2['dataSize']+st2['indexSize'])/MB:.1f} MB")
