"""
Precompute AI price verdicts for ALL listings (full DB), so that:
  • filters „Tylko okazje / W normie / Ukryj zawyżone” work on the whole base (not on a 1000-item batch),
  • verdicts are stable and identical for every user / page load.

Writes per listing: ai_verdict (deal|normal|over|outlier|none|noprice), ai_delta_pct, ai_median_ppm2,
                    ai_sample_n, ai_scope (human text), ai_computed_at.

Logic (mirrors szukaj.html computeAiVerdicts, but with geo for houses):
  dom:        similar houses (area ±30 %, same market) within 8→16→24 km, ≥5; fallback same city (≥5); else none
  mieszkanie: city (+market_type if ≥5), fallback geo 10→20→30 km (area ±30 %); else none
  dzialka:    city + dzialka_type, fallback geo 10→20→30 km (same dzialka_type); else none
  other:      city, fallback geo 10→20→30 km; else none
Thresholds: ≤-50 outlier | ≤-12 deal | <12 normal | <35 over | ≥35 outlier

Usage: MONGODB_URI=... python scripts/compute_ai_verdicts.py [--dry-run]
"""
import argparse, math, os, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne

MIN_PPM2 = {"mieszkanie": 500, "dom": 300, "dzialka": 3, "lokal": 500, "garaz": 100, "magazyn": 100}
GEO_STEPS = {"dom": [8, 16, 24]}
DEFAULT_GEO_STEPS = [10, 20, 30]
CELL = 0.35  # stopnie; 3x3 komórki pokrywają ≥24 km w każdą stronę


def hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def cell_of(lat, lng):
    return (int(lat // CELL), int(lng // CELL))


def verdict_for(delta):
    if delta <= -50:
        return "outlier"
    if delta <= -12:
        return "deal"
    if delta < 12:
        return "normal"
    if delta < 35:
        return "over"
    return "outlier"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    t0 = time.time()
    proj = {"_id": 1, "type": 1, "transaction_type": 1, "city": 1, "price": 1, "price_pm2": 1, "area_m2": 1,
            "lat": 1, "lng": 1, "market_type": 1, "dzialka_type": 1, "ai_verdict": 1, "ai_delta_pct": 1}
    docs = list(db.listings.find({}, proj))
    print(f"Loaded {len(docs)} listings in {time.time()-t0:.1f}s")

    def norm(d):
        t = (d.get("type") or "").lower()
        d["_t"] = t
        d["_tr"] = (d.get("transaction_type") or "sprzedaz").lower()
        d["_c"] = (d.get("city") or "").strip().lower()
        try:
            ppm = float(d.get("price_pm2") or 0)
        except (TypeError, ValueError):
            ppm = 0
        d["_ppm"] = ppm if ppm >= MIN_PPM2.get(t, 100) else 0
        try:
            d["_a"] = float(d.get("area_m2") or 0)
        except (TypeError, ValueError):
            d["_a"] = 0
        d["_lat"] = d.get("lat") if isinstance(d.get("lat"), (int, float)) else None
        d["_lng"] = d.get("lng") if isinstance(d.get("lng"), (int, float)) else None
        d["_m"] = (d.get("market_type") or "").lower()
        d["_p"] = (d.get("dzialka_type") or "unknown").lower() if t == "dzialka" else ""
        return d

    for d in docs:
        norm(d)
    valid = [d for d in docs if d["_ppm"] > 0]

    # ── Grupy miejskie ───────────────────────────────────────────────────────
    city_groups = defaultdict(list)       # (t, tr, c, p) -> [ppm]
    city_market = defaultdict(list)       # (t, tr, c, m) -> [ppm]   (mieszkania)
    grid = defaultdict(list)              # (t, tr, cell) -> [doc]
    for d in valid:
        city_groups[(d["_t"], d["_tr"], d["_c"], d["_p"])].append(d["_ppm"])
        if d["_t"] == "mieszkanie" and d["_m"]:
            city_market[(d["_t"], d["_tr"], d["_c"], d["_m"])].append(d["_ppm"])
        if d["_lat"] is not None and d["_lng"] is not None:
            grid[(d["_t"], d["_tr"], cell_of(d["_lat"], d["_lng"]))].append(d)

    def median_or_none(arr, n=5):
        return (statistics.median(arr), len(arr)) if len(arr) >= n else (None, len(arr))

    def geo_median(d, steps, same_area=True, same_market=True):
        if d["_lat"] is None or d["_lng"] is None:
            return None
        cx, cy = cell_of(d["_lat"], d["_lng"])
        cands = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cands.extend(grid.get((d["_t"], d["_tr"], (cx + dx, cy + dy)), ()))
        lo, hi = d["_a"] * 0.7, d["_a"] * 1.3
        sim = []
        for x in cands:
            if x is d or x["_ppm"] <= 0:
                continue
            if same_area and d["_a"] > 0 and not (lo <= x["_a"] <= hi):
                continue
            if same_market and d["_m"] and x["_m"] and x["_m"] != d["_m"]:
                continue
            if d["_t"] == "dzialka" and x["_p"] != d["_p"]:
                continue
            # duplikat tej samej oferty z innego portalu (ta sama cena+metraż) — nie liczymy
            if x.get("price") == d.get("price") and round(x["_a"]) == round(d["_a"]):
                continue
            x_dist = hav(d["_lat"], d["_lng"], x["_lat"], x["_lng"])
            sim.append((x_dist, x["_ppm"]))
        for km in steps:
            near = [ppm for dist, ppm in sim if dist <= km]
            if len(near) >= 5:
                return statistics.median(near), len(near), f"w promieniu {km} km"
        return None

    now = datetime.now(timezone.utc).isoformat()
    updates, stats = [], defaultdict(int)
    for d in docs:
        price = d.get("price") or 0
        res = None  # (median, n, scope)
        if not d["_ppm"]:
            v = "noprice" if not price else "none"
            fields = {"ai_verdict": v, "ai_delta_pct": 0, "ai_median_ppm2": None, "ai_sample_n": 0,
                      "ai_scope": "brak ceny" if v == "noprice" else "brak ceny za m²", "ai_computed_at": now}
        else:
            t = d["_t"]
            city_label = d.get("city") or ""
            if t == "dom":
                res = geo_median(d, GEO_STEPS["dom"])
                if not res:
                    med, n = median_or_none(city_groups[(t, d["_tr"], d["_c"], d["_p"])])
                    if med:
                        res = (med, n, f"w {city_label}")
            else:
                med, n = (None, 0)
                if t == "mieszkanie" and d["_m"]:
                    med, n = median_or_none(city_market[(t, d["_tr"], d["_c"], d["_m"])])
                if not med:
                    med, n = median_or_none(city_groups[(t, d["_tr"], d["_c"], d["_p"])])
                if med:
                    res = (med, n, f"w {city_label}")
                else:
                    res = geo_median(d, DEFAULT_GEO_STEPS, same_area=(t in ("mieszkanie", "dom")), same_market=(t == "mieszkanie"))
            if res:
                med, n, scope = res
                delta = (d["_ppm"] - med) / med * 100
                v = verdict_for(delta)
                fields = {"ai_verdict": v, "ai_delta_pct": round(delta), "ai_median_ppm2": round(med),
                          "ai_sample_n": n, "ai_scope": scope, "ai_computed_at": now}
            else:
                fields = {"ai_verdict": "none", "ai_delta_pct": 0, "ai_median_ppm2": None, "ai_sample_n": 0,
                          "ai_scope": "za mało podobnych ofert w okolicy", "ai_computed_at": now}
        stats[fields["ai_verdict"]] += 1
        if d.get("ai_verdict") != fields["ai_verdict"] or d.get("ai_delta_pct") != fields["ai_delta_pct"] or not d.get("ai_verdict"):
            updates.append(UpdateOne({"_id": d["_id"]}, {"$set": fields}))

    print(f"Computed in {time.time()-t0:.1f}s. Verdicts: {dict(stats)}. Changed: {len(updates)}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return
    done = 0
    for i in range(0, len(updates), 1000):
        done += db.listings.bulk_write(updates[i:i + 1000], ordered=False).modified_count
    print(f"✅ Updated {done} listings in {time.time()-t0:.1f}s")
    try:
        db.listings.create_index([("ai_verdict", 1), ("type", 1), ("transaction_type", 1)], background=True)
    except Exception as e:
        print("index:", e)


if __name__ == "__main__":
    main()
