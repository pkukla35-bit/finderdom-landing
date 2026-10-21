#!/usr/bin/env python3
"""
Tygodniowe raporty rynkowe FinderDom.pl — generowane z WŁASNYCH danych (oferty ze wszystkich portali + RCN).

Dla każdego z TOP miast (wg liczby aktywnych ofert) i typu (mieszkanie / dom) tworzy statyczną stronę HTML:
  raporty/<miasto>-<typ>-tydzien-<WW>-<RRRR>.html
oraz indeks raporty.html i wpisy w sitemap.xml. Historia tygodniowa w data/reports/history.json (trend, zmiana t/t).

Uruchamiane co poniedziałek przez GitHub Actions (.github/workflows/market-reports.yml), wynik commitowany do repo.
Użycie lokalne:  MONGODB_URI=... python scripts/market_reports.py [--cities 20] [--dry-run]
"""
import argparse
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "raporty"
HIST_PATH = ROOT / "data" / "reports" / "history.json"
SITEMAP = ROOT / "sitemap.xml"
DOMAIN = "https://www.finderdom.pl"

TYPE_LABEL = {"mieszkanie": ("mieszkania", "Mieszkania", "lokal"), "dom": ("domy", "Domy", "dom")}
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]
PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def slugify(s: str) -> str:
    s = s.translate(PL).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_pct(x, signed=True) -> str:
    if x is None:
        return "—"
    s = f"{x:+.1f}%" if signed else f"{x:.1f}%"
    return s.replace(".", ",")


def pl_date(d: datetime) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, int(len(xs) * p)))]


# --------------------------------------------------------------------------------------------------
# DANE
# --------------------------------------------------------------------------------------------------
def top_cities(db, n: int):
    pipe = [
        {"$match": {"type": {"$regex": "^mieszkanie", "$options": "i"}, "transaction_type": "sprzedaz",
                    "is_stale": {"$ne": True}, "price": {"$gt": 50000}, "city": {"$type": "string", "$ne": ""}}},
        {"$group": {"_id": "$city", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": n * 2},
    ]
    seen, out = set(), []
    for r in db.listings.aggregate(pipe):
        key = slugify(r["_id"])
        if key in seen or len(key) < 3:
            continue
        seen.add(key)
        out.append(r["_id"])
        if len(out) >= n:
            break
    return out


def load_active(db, city: str, ptype: str):
    q = {"city": city, "type": {"$regex": f"^{ptype}", "$options": "i"}, "transaction_type": "sprzedaz",
         "is_stale": {"$ne": True}, "price": {"$gt": 50000}, "area_m2": {"$gt": 10}}
    proj = {"price": 1, "area_m2": 1, "rooms": 1, "district": 1, "seller_type": 1, "market_type": 1,
            "added_at": 1, "portals": 1, "sources": 1, "external_id": 1, "title": 1, "price_pm2": 1, "build_year": 1}
    docs = list(db.listings.find(q, proj))
    for d in docs:
        d["ppm2"] = d.get("price_pm2") or (d["price"] / d["area_m2"] if d.get("area_m2") else None)
    # odrzuć skrajne wartości (błędne ceny, działki wpisane jako domy itp.)
    pp = [d["ppm2"] for d in docs if d["ppm2"]]
    lo, hi = pct(pp, 0.01), pct(pp, 0.99)
    if lo and hi:
        docs = [d for d in docs if d["ppm2"] and lo <= d["ppm2"] <= hi]
    return docs


def rcn_stats(db, city: str, ptype: str):
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    q = {"city": {"$regex": f"^{re.escape(city)}$", "$options": "i"}, "rodzaj_nieruchomosci": TYPE_LABEL[ptype][2],
         "data_zawarcia": {"$gte": since}, "cena_brutto": {"$gt": 20000}}
    docs = list(db.rcn_transactions.find(q, {"cena_brutto": 1, "cena_m2": 1, "powierzchnia": 1, "data_zawarcia": 1,
                                             "rodzaj_rynku": 1}))
    ppm = []
    for d in docs:
        v = d.get("cena_m2") or (d["cena_brutto"] / d["powierzchnia"] if d.get("powierzchnia") else None)
        if v and 500 < v < 80000:
            ppm.append(v)
    if len(ppm) < 10:
        return None
    return {"n": len(ppm), "median_ppm2": median(ppm), "p25": pct(ppm, 0.25), "p75": pct(ppm, 0.75),
            "median_price": median([d["cena_brutto"] for d in docs]),
            "last": max((d.get("data_zawarcia") or "") for d in docs)}


def compute(db, city: str, ptype: str, week_start: datetime, hist: list):
    docs = load_active(db, city, ptype)
    if len(docs) < 40:
        return None
    ws = week_start.isoformat()
    prev_ws = (week_start - timedelta(days=7)).isoformat()
    pp = [d["ppm2"] for d in docs]
    new_week = [d for d in docs if (d.get("added_at") or "") >= ws]
    new_prev = [d for d in docs if prev_ws <= (d.get("added_at") or "") < ws]

    def bucket_rooms(r):
        if not r:
            return None
        return "4+" if r >= 4 else str(int(r))

    rooms = {}
    if ptype == "mieszkanie":
        for d in docs:
            b = bucket_rooms(d.get("rooms"))
            if b:
                rooms.setdefault(b, []).append(d)
    rooms_rows = [{"label": k, "n": len(v), "median_ppm2": median([x["ppm2"] for x in v]),
                   "median_price": median([x["price"] for x in v]), "median_area": median([x.get("area_m2") for x in v])}
                  for k, v in sorted(rooms.items(), key=lambda kv: kv[0]) if len(v) >= 5]

    dist = {}
    for d in docs:
        dn = (d.get("district") or "").strip()
        if dn and dn.lower() != city.lower():
            dist.setdefault(dn, []).append(d["ppm2"])
    dist_rows = sorted([{"name": k, "n": len(v), "median_ppm2": median(v)} for k, v in dist.items() if len(v) >= 6],
                       key=lambda r: -r["median_ppm2"])

    private = sum(1 for d in docs if (d.get("seller_type") or "").lower() in ("prywatna", "private", "prywatny"))
    primary = sum(1 for d in docs if (d.get("market_type") or "").lower().startswith("pierwot"))
    with_market = sum(1 for d in docs if d.get("market_type"))
    multi = sum(1 for d in docs if len(d.get("portals") or d.get("sources") or []) >= 2)

    # histogram ppm2 (8 koszyków między p5 a p95)
    lo, hi = pct(pp, 0.05), pct(pp, 0.95)
    hist_b = []
    if lo and hi and hi > lo:
        step = (hi - lo) / 8
        for i in range(8):
            a, b = lo + i * step, lo + (i + 1) * step
            cnt = sum(1 for x in pp if (a <= x < b) or (i == 7 and x == b))
            hist_b.append({"from": a, "to": b, "n": cnt})

    med = median(pp)
    prev = hist[-1] if hist else None
    wow = ((med - prev["median_ppm2"]) / prev["median_ppm2"] * 100) if prev and prev.get("median_ppm2") else None
    m4 = hist[-4] if len(hist) >= 4 else None
    mom = ((med - m4["median_ppm2"]) / m4["median_ppm2"] * 100) if m4 and m4.get("median_ppm2") else None

    rcn = rcn_stats(db, city, ptype)
    gap = ((med - rcn["median_ppm2"]) / rcn["median_ppm2"] * 100) if rcn else None

    cheapest = sorted(docs, key=lambda d: d["ppm2"])[:5]
    return {
        "n": len(docs), "median_ppm2": med, "p25": pct(pp, 0.25), "p75": pct(pp, 0.75),
        "median_price": median([d["price"] for d in docs]), "median_area": median([d.get("area_m2") for d in docs]),
        "new_week": len(new_week), "new_prev": len(new_prev), "wow": wow, "mom": mom,
        "rooms": rooms_rows, "districts": dist_rows, "private_pct": private / len(docs) * 100,
        "primary_pct": (primary / with_market * 100) if with_market else None, "multi_pct": multi / len(docs) * 100,
        "hist": hist_b, "rcn": rcn, "gap": gap,
        "cheapest": [{"id": d.get("external_id"), "title": (d.get("title") or "")[:70], "ppm2": d["ppm2"],
                      "price": d["price"], "area": d.get("area_m2"), "district": d.get("district")} for d in cheapest],
    }


# --------------------------------------------------------------------------------------------------
# KOMENTARZ (z liczb, bez wymyślania)
# --------------------------------------------------------------------------------------------------
def commentary(city, ptype, s, week_no):
    lab = TYPE_LABEL[ptype][0]
    out = []
    out.append(f"W tygodniu {week_no} w wyszukiwarce FinderDom aktywnych było <strong>{fmt_int(s['n'])} ofert sprzedaży</strong> "
               f"({lab}) w mieście {escape(city)}. Mediana ceny ofertowej to <strong>{fmt_int(s['median_ppm2'])} zł/m²</strong>; "
               f"połowa ofert mieści się w przedziale {fmt_int(s['p25'])}–{fmt_int(s['p75'])} zł/m².")
    if s["wow"] is not None:
        if abs(s["wow"]) < 0.5:
            out.append("Względem poprzedniego tygodnia mediana praktycznie się nie zmieniła "
                       f"({fmt_pct(s['wow'])}) — rynek jest stabilny.")
        else:
            kier = "wzrosła" if s["wow"] > 0 else "spadła"
            out.append(f"Względem poprzedniego tygodnia mediana <strong>{kier} o {fmt_pct(abs(s['wow']), signed=False)}</strong>.")
    if s["mom"] is not None:
        out.append(f"W horyzoncie 4 tygodni zmiana wynosi {fmt_pct(s['mom'])}.")
    if s["new_prev"]:
        d = (s["new_week"] - s["new_prev"]) / s["new_prev"] * 100
        out.append(f"Nowych ogłoszeń pojawiło się <strong>{fmt_int(s['new_week'])}</strong> (tydzień wcześniej {fmt_int(s['new_prev'])}, "
                   f"zmiana {fmt_pct(d)}) — {'podaż rośnie' if d > 10 else ('podaż maleje' if d < -10 else 'podaż bez większych zmian')}.")
    else:
        out.append(f"Nowych ogłoszeń w tym tygodniu: <strong>{fmt_int(s['new_week'])}</strong>.")
    if s["rcn"] and s["gap"] is not None:
        out.append(f"Dla porównania mediana <strong>cen transakcyjnych</strong> z aktów notarialnych (RCN, ostatnie 12 mies., "
                   f"{fmt_int(s['rcn']['n'])} transakcji) to {fmt_int(s['rcn']['median_ppm2'])} zł/m² — ceny ofertowe są "
                   f"{'wyższe' if s['gap'] > 0 else 'niższe'} o {fmt_pct(abs(s['gap']), signed=False)}. "
                   + ("To typowa „poduszka negocjacyjna” — sprzedający zostawiają miejsce na targowanie." if 3 <= s["gap"] <= 15
                      else ("Różnica jest duża — warto negocjować odważniej niż zwykle." if s["gap"] > 15
                            else "Różnica jest mała — oferty są wyceniane blisko realnych cen sprzedaży.")))
    if s["districts"]:
        hi, lo = s["districts"][0], s["districts"][-1]
        if hi is not lo:
            out.append(f"Najdroższa dzielnica to <strong>{escape(hi['name'])}</strong> ({fmt_int(hi['median_ppm2'])} zł/m²), "
                       f"najtańsza <strong>{escape(lo['name'])}</strong> ({fmt_int(lo['median_ppm2'])} zł/m²) — "
                       f"różnica {fmt_pct((hi['median_ppm2'] / lo['median_ppm2'] - 1) * 100, signed=False)}.")
    out.append(f"Oferty bezpośrednio od właścicieli (bez pośrednika) stanowią {fmt_pct(s['private_pct'], signed=False)} rynku"
               + (f", rynek pierwotny {fmt_pct(s['primary_pct'], signed=False)} ofert z określonym rynkiem." if s["primary_pct"] is not None else "."))
    return out


# --------------------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------------------
def nav_html(active="raporty"):
    def a(href, label, key):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    return f"""<nav class="topnav"><div class="nav-inner"><a href="/" class="logo">🏠 FinderDom.pl</a><div class="nav-links">
{a('/szukaj', 'Wyszukiwarka', 'szukaj')}{a('/wycena', '💰 Wyceń nieruchomość', 'wycena')}{a('/raporty', '📊 Raporty rynkowe', 'raporty')}{a('/blog', '📝 Blog', 'blog')}{a('/pomoc', '💬 Pomoc', 'pomoc')}
<span id="authWidget" style="display:inline-flex;gap:8px;align-items:center;margin-left:auto"></span></div></div></nav>"""


FOOTER = """<footer><div>© 2026 FinderDom.pl · dane własne: oferty ze wszystkich portali + Rejestr Cen Nieruchomości (GUGiK)</div>
<div style="display:flex;flex-wrap:wrap;gap:16px"><a href="/">Strona główna</a><a href="/raporty">Raporty</a><a href="/blog">Blog</a><a href="/pomoc">Pomoc</a><a href="/regulamin">Regulamin</a><a href="/polityka-prywatnosci">Polityka prywatności</a><a href="mailto:kontakt@finderdom.pl">Kontakt</a></div></footer>
<script src="/scripts/ads.js" defer></script>
<script>(function(){const el=document.getElementById('authWidget');if(!el)return;const t=localStorage.getItem('finderdom_token');
el.innerHTML=t?'<a href="/konto" style="color:#FFB800;font-weight:800">👤 Moje konto</a>':'<a href="/logowanie" style="color:#c5d0e6">Zaloguj</a>';})();</script>"""

HEAD_TPL = """<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title><meta name="description" content="{desc}"><link rel="canonical" href="{url}">
<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}"><meta property="og:url" content="{url}"><meta property="og:type" content="article"><meta property="og:site_name" content="FinderDom.pl">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/scripts/blog.css">
<style>
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}}
.kpi{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:14px 16px}}
.kpi .v{{font-size:24px;font-weight:900;color:#fff;line-height:1.1}}.kpi .l{{font-size:12px;color:#8ba3d4;margin-top:4px}}
.kpi .d{{font-size:12px;font-weight:800;margin-top:4px}}.up{{color:#f87171}}.down{{color:#4ade80}}.flat{{color:#8ba3d4}}
.bars{{display:flex;align-items:flex-end;gap:6px;height:120px;margin:12px 0 4px}}.bar{{flex:1;background:linear-gradient(180deg,#FFB800,#b45309);border-radius:4px 4px 0 0;min-height:2px;position:relative}}
.bar span{{position:absolute;top:-18px;left:0;right:0;text-align:center;font-size:11px;color:#c5d0e6}}
.axis{{display:flex;gap:6px;font-size:10px;color:#8ba3d4}}.axis div{{flex:1;text-align:center}}
.spark{{width:100%;height:70px;display:block}}
table td,table th{{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.08);text-align:left}}table th{{color:#FFB800;font-size:12px;text-transform:uppercase;letter-spacing:.4px}}
.num{{text-align:right !important;font-variant-numeric:tabular-nums}}
.rep-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-top:20px}}
.rep-card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:16px;text-decoration:none;color:inherit;display:block}}
.rep-card:hover{{border-color:rgba(255,184,0,0.5)}}.rep-card h3{{margin:0 0 6px;font-size:16px;color:#fff}}.rep-card .m{{font-size:12px;color:#8ba3d4}}.rep-card .p{{font-size:20px;font-weight:900;color:#FFB800;margin:8px 0 2px}}
.arch{{font-size:13px;color:#8ba3d4;margin-top:6px}}.arch a{{color:#c5d0e6;margin-right:10px}}
.src{{font-size:12px;color:#8ba3d4;background:rgba(255,255,255,0.03);border-radius:10px;padding:12px 14px;margin-top:28px;line-height:1.5}}
</style>
<script type="application/ld+json">{ld}</script>
</head><body>"""


def sparkline(values):
    vals = [v for v in values if v]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    w, h = 600, 70
    pts = []
    for i, v in enumerate(vals):
        x = i * (w - 10) / (len(vals) - 1) + 5
        y = h - 8 - (v - lo) / rng * (h - 16)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-label="Trend mediany zł/m²">'
            f'<polyline fill="none" stroke="#FFB800" stroke-width="3" points="{" ".join(pts)}"/>'
            f'<text x="5" y="{h-1}" fill="#8ba3d4" font-size="11">{fmt_int(lo)} zł/m²</text>'
            f'<text x="{w-5}" y="12" fill="#8ba3d4" font-size="11" text-anchor="end">{fmt_int(hi)} zł/m²</text></svg>')


def delta_html(x, invert=False):
    if x is None:
        return '<div class="d flat">pierwszy pomiar</div>'
    cls = "flat" if abs(x) < 0.3 else ("up" if x > 0 else "down")
    return f'<div class="d {cls}">{fmt_pct(x)} t/t</div>'


def render_report(city, ptype, s, week_no, year, week_start, hist, slug_prev):
    lab_pl, lab_cap, _ = TYPE_LABEL[ptype]
    title = f"{lab_cap} {city} — ceny i rynek, tydzień {week_no}/{year} | Raport FinderDom"
    url = f"{DOMAIN}/raporty/{slugify(city)}-{ptype}-tydzien-{week_no:02d}-{year}"
    desc = (f"Mediana {fmt_int(s['median_ppm2'])} zł/m² ({fmt_pct(s['wow']) if s['wow'] is not None else 'pierwszy pomiar'} t/t), "
            f"{fmt_int(s['n'])} aktywnych ofert, {fmt_int(s['new_week'])} nowych w tym tygodniu. Ceny ofertowe vs transakcyjne z RCN, "
            f"dzielnice, metraże — {lab_pl} na sprzedaż w mieście {city}.")
    pub = week_start + timedelta(days=7)
    ld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                     "datePublished": pub.strftime("%Y-%m-%d"), "dateModified": pub.strftime("%Y-%m-%d"),
                     "author": {"@type": "Organization", "name": "FinderDom.pl"},
                     "publisher": {"@type": "Organization", "name": "FinderDom.pl", "logo": {"@type": "ImageObject", "url": f"{DOMAIN}/favicon.svg"}},
                     "mainEntityOfPage": url, "description": desc, "image": f"{DOMAIN}/marketing/logo-1200x1200.png"}, ensure_ascii=False)
    h = [HEAD_TPL.format(title=escape(title), desc=escape(desc), url=url, ld=ld), nav_html(), "<main>"]
    h.append(f'<div class="breadcrumb"><a href="/">Strona główna</a> · <a href="/raporty">Raporty rynkowe</a> · {escape(city)} · {lab_pl}</div>')
    h.append(f"<h1>{lab_cap} w mieście {escape(city)} — raport tygodniowy {week_no}/{year}</h1>")
    h.append(f'<div class="lead">Co się dzieje z cenami {lab_pl} w mieście {escape(city)}? Analiza {fmt_int(s["n"])} aktywnych ofert sprzedaży '
             f'ze wszystkich portali ogłoszeniowych oraz cen transakcyjnych z aktów notarialnych. Dane za tydzień {pl_date(week_start)} – {pl_date(week_start + timedelta(days=6))}.</div>')
    h.append(f'<div class="meta">📅 {pl_date(pub)} · 📊 dane własne FinderDom · 🔁 aktualizacja co tydzień</div>')

    # KPI
    h.append('<div class="kpis">')
    h.append(f'<div class="kpi"><div class="v">{fmt_int(s["median_ppm2"])} zł</div><div class="l">mediana ceny za m²</div>{delta_html(s["wow"])}</div>')
    h.append(f'<div class="kpi"><div class="v">{fmt_int(s["median_price"])} zł</div><div class="l">mediana ceny oferty</div><div class="d flat">mediana metrażu {fmt_int(s["median_area"])} m²</div></div>')
    h.append(f'<div class="kpi"><div class="v">{fmt_int(s["n"])}</div><div class="l">aktywnych ofert sprzedaży</div><div class="d flat">{fmt_pct(s["multi_pct"], signed=False)} na ≥2 portalach</div></div>')
    nw_d = ((s["new_week"] - s["new_prev"]) / s["new_prev"] * 100) if s["new_prev"] else None
    h.append(f'<div class="kpi"><div class="v">{fmt_int(s["new_week"])}</div><div class="l">nowych ogłoszeń w tym tygodniu</div>{delta_html(nw_d)}</div>')
    if s["rcn"]:
        h.append(f'<div class="kpi"><div class="v">{fmt_int(s["rcn"]["median_ppm2"])} zł</div><div class="l">mediana cen transakcyjnych (RCN, 12 mies.)</div>'
                 f'<div class="d {"up" if s["gap"] and s["gap"] > 0 else "down"}">oferty {fmt_pct(s["gap"])} vs transakcje</div></div>')
    h.append('<div class="kpi"><div class="v">' + fmt_pct(s["private_pct"], signed=False) + '</div><div class="l">ofert bezpośrednio od właścicieli</div></div>')
    h.append("</div>")

    h.append("<article>")
    h.append("<h2>📝 Komentarz do danych</h2>")
    for p in commentary(city, ptype, s, week_no):
        h.append(f"<p>{p}</p>")

    if len(hist) >= 1:
        vals = [x.get("median_ppm2") for x in hist[-11:]] + [s["median_ppm2"]]
        h.append("<h2>📈 Trend mediany zł/m² (ostatnie tygodnie)</h2>")
        h.append(sparkline(vals))
        h.append('<table><tr><th>Tydzień</th><th class="num">Mediana zł/m²</th><th class="num">Aktywne oferty</th><th class="num">Nowe</th></tr>')
        rows = hist[-7:] + [{"week": f"{week_no:02d}/{year}", "median_ppm2": s["median_ppm2"], "n": s["n"], "new_week": s["new_week"]}]
        for r in rows:
            h.append(f'<tr><td>{escape(str(r["week"]))}</td><td class="num">{fmt_int(r.get("median_ppm2"))}</td><td class="num">{fmt_int(r.get("n"))}</td><td class="num">{fmt_int(r.get("new_week"))}</td></tr>')
        h.append("</table>")

    if s["hist"]:
        mx = max(b["n"] for b in s["hist"]) or 1
        h.append("<h2>📊 Rozkład cen ofertowych za m²</h2>")
        h.append('<div class="bars">' + "".join(f'<div class="bar" style="height:{max(2, b["n"] / mx * 100):.0f}%"><span>{b["n"]}</span></div>' for b in s["hist"]) + "</div>")
        h.append('<div class="axis">' + "".join(f'<div>{fmt_int(b["from"] / 1000)}–{fmt_int(b["to"] / 1000)} tys.</div>' for b in s["hist"]) + "</div>")
        h.append(f'<p style="font-size:13px;color:#8ba3d4">Liczba ofert w przedziałach cenowych (zł/m²), zakres od 5. do 95. percentyla. 25% najtańszych ofert kosztuje do {fmt_int(s["p25"])} zł/m², 25% najdroższych od {fmt_int(s["p75"])} zł/m².</p>')

    if s["rooms"]:
        h.append("<h2>🚪 Ceny według liczby pokoi</h2>")
        h.append('<table><tr><th>Pokoje</th><th class="num">Ofert</th><th class="num">Mediana zł/m²</th><th class="num">Mediana ceny</th><th class="num">Mediana m²</th></tr>')
        for r in s["rooms"]:
            h.append(f'<tr><td>{r["label"]}</td><td class="num">{fmt_int(r["n"])}</td><td class="num">{fmt_int(r["median_ppm2"])}</td><td class="num">{fmt_int(r["median_price"])} zł</td><td class="num">{fmt_int(r["median_area"])}</td></tr>')
        h.append("</table>")

    if s["districts"]:
        h.append("<h2>📍 Dzielnice — od najdroższej do najtańszej</h2>")
        h.append('<table><tr><th>Dzielnica</th><th class="num">Ofert</th><th class="num">Mediana zł/m²</th><th class="num">vs miasto</th></tr>')
        for r in s["districts"][:15]:
            d = (r["median_ppm2"] / s["median_ppm2"] - 1) * 100
            h.append(f'<tr><td>{escape(r["name"])}</td><td class="num">{fmt_int(r["n"])}</td><td class="num">{fmt_int(r["median_ppm2"])}</td><td class="num {"up" if d > 0 else "down"}">{fmt_pct(d)}</td></tr>')
        h.append("</table>")
        h.append('<p style="font-size:13px;color:#8ba3d4">Uwzględniono dzielnice z co najmniej 6 aktywnymi ofertami.</p>')

    if s["rcn"]:
        h.append("<h2>🏛️ Ceny ofertowe a ceny transakcyjne (RCN)</h2>")
        h.append(f'<p>Rejestr Cen Nieruchomości (GUGiK) zawiera ceny z aktów notarialnych. W ostatnich 12 miesiącach odnotowano <strong>{fmt_int(s["rcn"]["n"])} transakcji</strong> '
                 f'({TYPE_LABEL[ptype][0]}) w mieście {escape(city)}; mediana to <strong>{fmt_int(s["rcn"]["median_ppm2"])} zł/m²</strong> '
                 f'(przedział {fmt_int(s["rcn"]["p25"])}–{fmt_int(s["rcn"]["p75"])}), mediana ceny całkowitej {fmt_int(s["rcn"]["median_price"])} zł. '
                 f'Aktualne oferty są wyceniane <strong>{fmt_pct(s["gap"])}</strong> względem tych transakcji. Ostatnia transakcja w rejestrze: {escape(s["rcn"]["last"])}.</p>')
        h.append('<div class="callout"><div class="callout-title">💡 Jak to wykorzystać</div><p style="margin:0">Kupujesz? Różnica między ceną ofertową a transakcyjną to realny punkt wyjścia do negocjacji. '
                 'Sprzedajesz? Oferta wyceniona blisko mediany transakcyjnej sprzedaje się szybciej. <a href="/wycena" style="color:#FFB800">Wycena PDF FinderDom</a> porównuje konkretne mieszkanie z obiema bazami.</p></div>')

    if s["cheapest"]:
        h.append("<h2>🔎 Najniższe ceny za m² w tym tygodniu</h2>")
        h.append('<table><tr><th>Oferta</th><th>Dzielnica</th><th class="num">m²</th><th class="num">Cena</th><th class="num">zł/m²</th></tr>')
        for c in s["cheapest"]:
            link = f'<a href="/oferta?id=otodom-{escape(str(c["id"]))}" style="color:#c5d0e6">{escape(c["title"] or "Oferta")}</a>'
            h.append(f'<tr><td>{link}</td><td>{escape(c["district"] or "—")}</td><td class="num">{fmt_int(c["area"])}</td><td class="num">{fmt_int(c["price"])} zł</td><td class="num">{fmt_int(c["ppm2"])}</td></tr>')
        h.append("</table>")
        h.append('<p style="font-size:13px;color:#8ba3d4">Niska cena za m² bywa sygnałem okazji, ale też problemów (stan prawny, lokalizacja, stan techniczny). Sprawdź analizę AI przy każdej ofercie.</p>')

    h.append("</article>")
    h.append(f'<div class="cta-box"><h3>🔍 Zobacz te oferty na żywo</h3><p>Wszystkie {fmt_int(s["n"])} ofert z tego raportu — z filtrem dzielnicy, pokoi i ceny za m², z analizą AI i alertami o nowych ogłoszeniach.</p>'
             f'<a href="/szukaj?q={escape(city)}&type={ptype}&trans=sprzedaz" class="btn">Otwórz wyszukiwarkę: {lab_pl} {escape(city)} →</a></div>')
    if slug_prev:
        h.append(f'<p class="arch">Poprzedni raport: <a href="/raporty/{slug_prev}">tydzień {hist[-1]["week"]}</a> · <a href="/raporty">wszystkie raporty</a></p>')
    h.append('<div class="src"><strong>Metodologia.</strong> Źródło: aktywne ogłoszenia sprzedaży zebrane przez FinderDom ze wszystkich głównych portali (duplikaty tej samej nieruchomości na kilku portalach liczone raz), '
             'stan na poniedziałek rano. Odrzucamy 1% skrajnych wartości zł/m² (błędne ceny). Mediana jest odporna na pojedyncze oferty luksusowe. Ceny transakcyjne: Rejestr Cen Nieruchomości (GUGiK, usługa WFS), ostatnie 12 miesięcy. '
             'Raport ma charakter informacyjny i nie stanowi wyceny ani rekomendacji inwestycyjnej.</div>')
    h.append('<div class="fd-ad-slot" data-slot="article-bottom" data-format="horizontal"></div></main>')
    h.append(FOOTER + "</body></html>")
    return "\n".join(h), url, title, desc


def render_index(entries, week_no, year, generated):
    title = "Raporty rynkowe — ceny mieszkań i domów w polskich miastach, co tydzień | FinderDom.pl"
    desc = ("Tygodniowe raporty cen nieruchomości z własnych danych FinderDom: mediana zł/m², nowe ogłoszenia, dzielnice, "
            "ceny ofertowe vs transakcyjne z RCN. " + ", ".join(sorted({e["city"] for e in entries})[:12]) + "…")
    url = f"{DOMAIN}/raporty"
    ld = json.dumps({"@context": "https://schema.org", "@type": "CollectionPage", "name": title, "description": desc, "url": url}, ensure_ascii=False)
    h = [HEAD_TPL.format(title=escape(title), desc=escape(desc), url=url, ld=ld), nav_html(), '<main style="max-width:1100px">']
    h.append('<div class="breadcrumb"><a href="/">Strona główna</a> · Raporty rynkowe</div>')
    h.append("<h1>📊 Raporty rynkowe — co tydzień, z własnych danych</h1>")
    h.append('<div class="lead">Agregujemy ogłoszenia ze wszystkich portali i zestawiamy je z cenami z aktów notarialnych. '
             'Co poniedziałek publikujemy raport dla każdego dużego miasta: mediana zł/m², zmiana tydzień do tygodnia, nowe ogłoszenia, dzielnice, metraże i realne ceny transakcyjne.</div>')
    h.append(f'<div class="meta">📅 ostatnia aktualizacja: {pl_date(generated)} · tydzień {week_no}/{year} · {len(entries)} raportów</div>')
    by_city = {}
    for e in entries:
        by_city.setdefault(e["city"], []).append(e)
    for ptype in ("mieszkanie", "dom"):
        cur = [e for e in entries if e["ptype"] == ptype and e["latest"]]
        if not cur:
            continue
        h.append(f"<h2>{TYPE_LABEL[ptype][1]} — aktualne raporty</h2>")
        h.append('<div class="rep-grid">')
        for e in sorted(cur, key=lambda x: -x["n"]):
            wow = f'<span class="{"up" if e["wow"] and e["wow"] > 0.3 else ("down" if e["wow"] and e["wow"] < -0.3 else "flat")}">{fmt_pct(e["wow"]) if e["wow"] is not None else "nowy"}</span>'
            h.append(f'<a class="rep-card" href="/raporty/{e["slug"]}"><h3>{escape(e["city"])}</h3><div class="m">tydzień {e["week"]} · {fmt_int(e["n"])} ofert · {fmt_int(e["new_week"])} nowych</div>'
                     f'<div class="p">{fmt_int(e["median_ppm2"])} zł/m²</div><div class="m">{wow} tydzień do tygodnia</div></a>')
        h.append("</div>")
    arch = [e for e in entries if not e["latest"]]
    if arch:
        h.append("<h2>🗂️ Archiwum</h2>")
        for city in sorted(by_city):
            old = [e for e in by_city[city] if not e["latest"]]
            if old:
                h.append(f'<p class="arch"><strong style="color:#c5d0e6">{escape(city)}:</strong> ' + " ".join(
                    f'<a href="/raporty/{e["slug"]}">{TYPE_LABEL[e["ptype"]][0]} {e["week"]}</a>' for e in sorted(old, key=lambda x: x["slug"], reverse=True)[:16]) + "</p>")
    h.append('<div class="cta-box"><h3>🔔 Chcesz dostawać raport dla swojego miasta?</h3><p>Ustaw alert w wyszukiwarce — codziennie rano wyślemy nowe oferty pasujące do Twoich kryteriów.</p><a href="/szukaj" class="btn">Otwórz wyszukiwarkę →</a></div>')
    h.append("</main>" + FOOTER + "</body></html>")
    return "\n".join(h)


def update_sitemap(urls):
    if not SITEMAP.exists():
        return
    s = SITEMAP.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    add = [u for u in urls if f"<loc>{u}</loc>" not in s]
    if not add:
        return
    block = "".join(f"\n  <url><loc>{u}</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.7</priority></url>" for u in add)
    s = s.replace("</urlset>", block + "\n</urlset>")
    SITEMAP.write_text(s, encoding="utf-8")


# --------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB", "finderdom")]
    now = datetime.now(timezone.utc)
    # raport za MINIONY pełny tydzień (pon–nd); generowany w poniedziałek
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = this_monday - timedelta(days=7)
    iso_year, week_no, _ = week_start.isocalendar()
    week_key = f"{week_no:02d}/{iso_year}"

    history = json.loads(HIST_PATH.read_text(encoding="utf-8")) if HIST_PATH.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)
    HIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    cities = top_cities(db, args.cities)
    print(f"Tydzień {week_key} ({week_start.date()} – {(week_start + timedelta(days=6)).date()}), miasta: {cities}")
    entries, new_urls = [], []
    for city in cities:
        for ptype in ("mieszkanie", "dom"):
            key = f"{slugify(city)}-{ptype}"
            hist = [h for h in history.get(key, []) if h.get("week") != week_key]
            s = compute(db, city, ptype, week_start, hist)
            if not s:
                print(f"  - {key}: za mało ofert, pomijam")
                continue
            slug = f"{key}-tydzien-{week_no:02d}-{iso_year}"
            slug_prev = f"{key}-tydzien-{hist[-1]['week'].replace('/', '-')}" if hist else None
            html, url, title, desc = render_report(city, ptype, s, week_no, iso_year, week_start, hist, slug_prev)
            if not args.dry_run:
                (OUT_DIR / f"{slug}.html").write_text(html, encoding="utf-8")
            hist.append({"week": week_key, "median_ppm2": round(s["median_ppm2"]), "n": s["n"], "new_week": s["new_week"],
                         "median_price": round(s["median_price"]), "slug": slug})
            history[key] = hist[-60:]
            new_urls.append(url)
            print(f"  ✓ {slug}: n={s['n']} med={s['median_ppm2']:.0f} new={s['new_week']} wow={s['wow']}")

    # indeks: wszystkie raporty z historii
    for key, hist in history.items():
        city_slug, ptype = key.rsplit("-", 1)
        for i, hh in enumerate(hist):
            if "slug" not in hh:
                continue
            entries.append({"city": next((c for c in cities if slugify(c) == city_slug), city_slug.replace("-", " ").title()),
                            "ptype": ptype, "slug": hh["slug"], "week": hh["week"], "n": hh["n"], "new_week": hh.get("new_week", 0),
                            "median_ppm2": hh["median_ppm2"], "latest": i == len(hist) - 1,
                            "wow": ((hh["median_ppm2"] - hist[i - 1]["median_ppm2"]) / hist[i - 1]["median_ppm2"] * 100) if i > 0 and hist[i - 1].get("median_ppm2") else None})
    if not args.dry_run:
        (ROOT / "raporty.html").write_text(render_index(entries, week_no, iso_year, now), encoding="utf-8")
        HIST_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        update_sitemap([f"{DOMAIN}/raporty"] + new_urls)
    print(f"Gotowe: {len(new_urls)} raportów, {len(entries)} wpisów w indeksie.")


if __name__ == "__main__":
    main()
