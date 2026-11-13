#!/usr/bin/env python3
"""
Codzienny raport zdrowia importów (09:30 PL, pn–pt) → e-mail do admina przez SMTP z ustawień FinderDom (settings._id=email).

Sprawdza dla każdego portalu:
  1. czy strona listy odpowiada (HTTP 200 + da się odczytać oferty) — „portal żyje”,
  2. ile ofert weszło do bazy dziś / wczoraj (wg source),
  3. czy ostatni bieg workflow GitHub zakończył się sukcesem.
Oraz: rozmiar bazy, łączna liczba aktywnych ofert, wynajem/sprzedaż dziś.
Env: MONGODB_URI, MONGODB_DB, GITHUB_TOKEN, GITHUB_REPOSITORY, ALERT_EMAIL (domyślnie pkukla35@gmail.com)
"""
import json, os, re, smtplib, ssl, sys, urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PL = timezone(timedelta(hours=1))

PORTALS = [
    # (label, source w bazie, workflow file, URL testowy, funkcja licząca oferty w HTML)
    ("Otodom", "otodom", "scrape-otodom-regions.yml",
     "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie?limit=36", lambda h: h.count('"id":') if "__NEXT_DATA__" in h else 0),
    ("OLX", "olx", "scrape-olx.yml",
     "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/krakow/", lambda h: h.count('"url":"https://www.olx.pl/d/oferta') if "__PRERENDERED_STATE__" in h else 0),
    ("nieruchomosci-online", "nol", "scrape-nol.yml",
     "https://www.nieruchomosci-online.pl/szukaj.html?3,mieszkanie,sprzedaz,,Krak%C3%B3w", lambda h: len(re.findall(r'data-id="a\d+"', h))),
    ("Morizon", "morizon", "scrape-portals.yml",
     "https://www.morizon.pl/mieszkania/krakow/", lambda h: h.count('"@type": "Offer"') or h.count('"@type":"Offer"')),
    ("Gratka", "gratka", "scrape-portals.yml",
     "https://gratka.pl/nieruchomosci/mieszkania/krakow", lambda h: h.count('"@type": "Offer"') or h.count('"@type":"Offer"')),
    ("Domiporta", "domiporta", "scrape-portals.yml",
     "https://www.domiporta.pl/mieszkanie/sprzedam/malopolskie/krakow", lambda h: len(re.findall(r'data-detail-id="\d+"', h))),
    ("Adresowo", "adresowo", "scrape-portals.yml",
     "https://adresowo.pl/mieszkania/krakow/", lambda h: h.count("data-offer-card")),
    ("Gethome", "gethome", "scrape-portals.yml",
     "https://gethome.pl/mieszkania/na-sprzedaz/malopolskie/?sort=newest", lambda h: h.count('"slug":') if "__INITIAL_STATE__" in h else 0),
    ("OdWlasciciela", "odwlasciciela", "scrape-portals.yml",
     "https://odwlasciciela.pl/mieszkania/sprzedaz/malopolskie.html", lambda h: h.count('<article class="offers-item')),
    ("ListaPrzetargow", "listaprzetargow", "scrape-portals.yml",
     "https://listaprzetargow.pl/oferty/mieszkania?sort=created_at-desc", lambda h: h.count('class="modernCard ')),
]


def fetch(url):
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", headers={"Accept-Language": "pl-PL,pl;q=0.9"}, timeout=40)
        return r.status_code, r.text
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except Exception as e:
            return 0, str(e)


def gh_last_run(wf):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo: return None
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=3&exclude_pull_requests=true",
                                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            runs = json.loads(r.read()).get("workflow_runs") or []
        done = [x for x in runs if x.get("status") == "completed"]
        if not done: return {"conclusion": "w toku", "url": runs[0]["html_url"] if runs else ""}
        return {"conclusion": done[0]["conclusion"], "url": done[0]["html_url"], "at": done[0]["created_at"]}
    except Exception as e:
        return {"conclusion": f"błąd API: {str(e)[:60]}", "url": ""}


def main():
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
    db = MongoClient(uri, serverSelectionTimeoutMS=10000)[os.environ.get("MONGODB_DB", "finderdom")]
    L = db.listings
    now = datetime.now(timezone.utc)
    today0 = now.astimezone(PL).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    yday0 = (now.astimezone(PL).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    week0 = (now.astimezone(PL).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)).astimezone(timezone.utc).isoformat()

    weekend = now.astimezone(PL).weekday() >= 5   # importy chodzą pn–pt — w weekend 0 ofert „dziś” to norma, nie awaria
    rows, problems, critical = [], [], []
    for label, src, wf, url, counter in PORTALS:
        code, html = fetch(url)
        n_live = counter(html) if code == 200 else 0
        live_ok = code == 200 and n_live > 0
        # sprzedaż i wynajem liczone osobno — awaria importu wynajmu nie może się „schować” za sprzedażą
        def cnt(extra):
            return L.count_documents({"source": src, **extra})
        today_s = cnt({"added_at": {"$gte": today0}, "transaction_type": {"$ne": "wynajem"}})
        today_r = cnt({"added_at": {"$gte": today0}, "transaction_type": "wynajem"})
        yday_s = cnt({"added_at": {"$gte": yday0, "$lt": today0}, "transaction_type": {"$ne": "wynajem"}})
        yday_r = cnt({"added_at": {"$gte": yday0, "$lt": today0}, "transaction_type": "wynajem"})
        week_r = cnt({"added_at": {"$gte": week0}, "transaction_type": "wynajem"})
        today = today_s + today_r
        run = gh_last_run(wf) or {}
        run_ok = run.get("conclusion") in ("success", "w toku")
        run_today = (run.get("at") or "") >= today0 and run.get("conclusion") == "success"   # job skończył się DZIŚ sukcesem
        rent_expected = week_r > 0          # portal normalnie dostarcza wynajem (w ostatnich 7 dniach)
        rent_ok = (not rent_expected) or today_r > 0
        fresh_ok = weekend or (today_s > 0 and rent_ok)
        # 🔴 CICHA AWARIA: job „success” dziś, a portal wczytał 0 ofert (4.11 Gratka+Morizon zmieniły HTML — joby zielone, new=0)
        # 12.11 fałszywy alarm ListaPrzetargow: nocny job o 04:25 nie znalazł NOWYCH (portal dodaje kilkadziesiąt/dzień, w dzień),
        # ale odświeżył 50 istniejących → parser działa. Dowód: last_seen_at ≥ dziś u ofert z tego źródła.
        parser_alive = L.find_one({"source": src, "last_seen_at": {"$gte": today0}}, {"_id": 1}) is not None
        silent_zero = (not weekend) and run_today and today == 0 and (yday_s + yday_r) >= 20 and not parser_alive   # małe portale (0 wczoraj też) → tylko ⚠️
        status = "🔴" if silent_zero else ("✅" if (live_ok and run_ok and fresh_ok) else ("⚠️" if live_ok else "❌"))
        if silent_zero:
            critical.append(f"{label}: job zakończył się sukcesem ({(run.get('at') or '')[11:16]} UTC), ale wczytał <b>0 ofert</b> (wczoraj {yday_s + yday_r}) — "
                            f"najpewniej zmiana strony portalu lub blokada; parser do sprawdzenia")
        elif status != "✅":
            why = []
            if today == 0 and parser_alive: why.append("0 nowych ofert dziś, ale parser działa (oferty z portalu odświeżone dziś) — czekamy na kolejne tury")
            if not live_ok: why.append(f"strona portalu: HTTP {code}, ofert w HTML: {n_live}")
            if not run_ok: why.append(f"workflow: {run.get('conclusion')}")
            if not weekend and today_s == 0:
                why.append("0 ofert SPRZEDAŻY dodanych dziś" + ("" if run_today else " (import jeszcze dziś nie biegł — GitHub opóźnia harmonogram)"))
            if not weekend and not rent_ok: why.append(f"0 ofert WYNAJMU dziś (wczoraj {yday_r}, w 7 dni {week_r})")
            problems.append(f"{label}: " + "; ".join(why))
        rows.append((status, label, f"HTTP {code} / {n_live} ofert", today_s, today_r, yday_s, yday_r, run.get("conclusion", "—"), run.get("url", "")))

    total_active = L.count_documents({"is_stale": {"$ne": True}})
    today_all = L.count_documents({"added_at": {"$gte": today0}})
    today_rent = L.count_documents({"added_at": {"$gte": today0}, "transaction_type": "wynajem"})
    active_rent = L.count_documents({"is_stale": {"$ne": True}, "transaction_type": "wynajem"})
    # Rodzaje nieruchomości osobno — 2.11 działki i lokale stały 3 dni (Apify wyłączone, scrapery zapisywały „działka …”),
    # a raport mówił „OK”, bo patrzył tylko na sumy per portal. Typ porównujemy bez polskich znaków.
    import unicodedata as _ud
    def _ascii(t):   # UWAGA: „ł” nie ma dekompozycji NFKD → bez tej zamiany „działka” stawało się „dziaka” i raport pokazywał 0 działek
        return _ud.normalize("NFKD", (t or "").replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode().lower()
    type_rows = []
    today_docs = list(L.find({"added_at": {"$gte": today0}}, {"type": 1}))
    yday_docs = list(L.find({"added_at": {"$gte": yday0, "$lt": today0}}, {"type": 1}))
    def _cnt(docs, key): return sum(1 for d in docs if _ascii(str(d.get("type"))).startswith(key))
    for label_t, key in (("🏢 Mieszkania", "mieszkanie"), ("🏠 Domy", "dom"), ("🌳 Działki", "dzialka"), ("🏪 Lokale", "lokal")):
        t_today, t_yday = _cnt(today_docs, key), _cnt(yday_docs, key)
        t_week = L.count_documents({"added_at": {"$gte": week0}, "type": {"$regex": "^" + key[:3], "$options": "i"}})
        ok_t = weekend or t_today > 0 or t_week == 0
        type_rows.append((("✅" if ok_t else "⚠️"), label_t, t_today, t_yday))
        if not ok_t:
            problems.append(f"{label_t}: 0 ofert dodanych dziś (wczoraj {t_yday})")
    st = db.command("dbStats"); size_mb = (st["dataSize"] + st["indexSize"]) / 1024 / 1024

    # 📍 Miasta biur (szkolenia/klienci): nowe oferty dziś per typ — 13.11 Szczecin miał 3 działki „dziś” i filtr przeznaczenia dawał 0,
    # a raport mówił „✅ Działki 141” (suma ogólnopolska). Działki = 0 w mieście biura → ⚠️.
    city_rows = []
    for cname in ("Szczecin", "Kraków", "Warszawa", "Wrocław", "Poznań", "Gdańsk"):
        cq = {"city": {"$regex": "^" + re.escape(cname) + "$", "$options": "i"}, "transaction_type": {"$ne": "wynajem"}}
        per = []
        for key in ("mieszkanie", "dom", "dzialka", "lokal"):
            tq = {"type": {"$regex": "^" + key[:3], "$options": "i"}}
            per.append((L.count_documents({**cq, **tq, "added_at": {"$gte": today0}}), L.count_documents({**cq, **tq, "added_at": {"$gte": yday0, "$lt": today0}})))
        city_rows.append((cname, per))
        # działki w granicach miasta to rzadkość (większość w gminach obok → promień); ⚠️ dopiero gdy 0 dziś I 0 wczoraj
        if not weekend and per[2][0] == 0 and per[2][1] == 0:
            problems.append(f"📍 {cname}: 0 działek dodanych dziś i wczoraj — sprawdź import działek OLX/Otodom dla miasta")

    # 🧪 Wycena PDF na żywo (4 przypadki: mieszkanie/dom/działka, Szczecin/Kraków/Warszawa) — klucz w settings.health (tworzony tu, czytany przez API)
    smoke_rows = []
    hcfg = db.settings.find_one({"_id": "health"}) or {}
    if not hcfg.get("smoke_key"):
        import secrets as _sec
        hcfg["smoke_key"] = _sec.token_urlsafe(32)
        db.settings.update_one({"_id": "health"}, {"$set": {"smoke_key": hcfg["smoke_key"], "created_at": datetime.now(timezone.utc)}}, upsert=True)
    for case in ("mieszkanie-szczecin", "dom-krakow", "dzialka-szczecin", "mieszkanie-warszawa"):
        try:
            rq = urllib.request.Request(f"https://www.finderdom.pl/api/health/valuation-smoke?case={case}", headers={"X-Health-Key": hcfg["smoke_key"], "User-Agent": "FinderDom-health"})
            with urllib.request.urlopen(rq, timeout=60) as r:
                res = json.loads(r.read().decode())
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        smoke_rows.append((case, res))
        if not res.get("ok"):
            critical.append(f"🧪 Wycena PDF ({case}): <b>{res.get('error') or 'brak PDF'}</b> {' · '.join(res.get('trace') or [])}")
    smoke_fail = sum(1 for _, r in smoke_rows if not r.get("ok"))

    date_s = now.astimezone(PL).strftime("%d.%m.%Y %H:%M")
    subject = ((f"🔴 FinderDom: WYCENA PDF nie działa ({smoke_fail}/4 testów)" if smoke_fail else f"🔴 FinderDom: {len(critical)} portal(e) wczytały 0 OFERT mimo udanego joba") if critical else
               ("✅ FinderDom: importy i wycena OK" if not problems else f"⚠️ FinderDom: {len(problems)} rzeczy do sprawdzenia")) + f" — {date_s}"
    def link(c, u):
        return f'<a href="{u}">{c}</a>' if u else str(c)
    def rent_cell(v, bold=False):
        col = "#b45309" if v == 0 else "#111"
        return f"<td style='padding:6px 10px;text-align:right;color:{col}'>{'<b>' if bold else ''}{v}{'</b>' if bold else ''}</td>"
    tr = "".join(
        f"<tr><td style='padding:6px 10px'>{s} <b>{l}</b></td><td style='padding:6px 10px'>{live}</td>"
        f"<td style='padding:6px 10px;text-align:right'><b>{ts}</b></td>{rent_cell(trn, True)}"
        f"<td style='padding:6px 10px;text-align:right;color:#666'>{ys}</td><td style='padding:6px 10px;text-align:right;color:#666'>{yr}</td>"
        f"<td style='padding:6px 10px'>{link(c, u)}</td></tr>"
        for s, l, live, ts, trn, ys, yr, c, u in rows)
    html = f"""<div style="font-family:Arial,sans-serif;max-width:720px;color:#111">
<h2 style="color:#0B1836;margin:0 0 6px">Raport zdrowia importów — {date_s}</h2>
{'<p style="margin:0 0 10px;color:#666;font-size:13px">Weekend — importy działają pn–pt, dzisiejsze „0” jest normalne; sprawdzono tylko dostępność portali i ostatnie biegi.</p>' if weekend else ''}
<p style="margin:0 0 14px;color:#444">Dziś dodano <b>{today_all}</b> ofert: sprzedaż <b>{today_all - today_rent}</b>, wynajem <b>{today_rent}</b>. Aktywnych w bazie: <b>{total_active:,}</b> (w tym wynajem {active_rent:,}). Rozmiar bazy: {size_mb:.0f} MB / 5 GB.</p>
<table style="border-collapse:collapse;margin:0 0 16px;font-size:13px"><thead><tr style="background:#f3f4f6"><th style="padding:5px 10px;text-align:left">Rodzaj</th><th style="padding:5px 10px;text-align:right">Dziś</th><th style="padding:5px 10px;text-align:right">Wczoraj</th></tr></thead><tbody>
{"".join(f"<tr><td style='padding:5px 10px'>{st} {lb}</td><td style='padding:5px 10px;text-align:right;color:{'#b45309' if td == 0 else '#111'}'><b>{td}</b></td><td style='padding:5px 10px;text-align:right;color:#666'>{ty}</td></tr>" for st, lb, td, ty in type_rows)}
</tbody></table>
<h3 style="color:#0B1836;margin:0 0 6px;font-size:15px">🧪 Wycena PDF — test na żywo</h3>
<table style="border-collapse:collapse;margin:0 0 16px;font-size:13px"><tbody>
{"".join(f"<tr><td style='padding:4px 10px'>{'✅' if r.get('ok') else '🔴'} {c}</td><td style='padding:4px 10px;color:{'#111' if r.get('ok') else '#b91c1c'}'>{('PDF ' + str(r.get('pdf_kb')) + ' KB, ' + str(r.get('seconds')) + ' s') if r.get('ok') else (r.get('error') or 'brak PDF')}</td></tr>" for c, r in smoke_rows)}
</tbody></table>
<h3 style="color:#0B1836;margin:0 0 6px;font-size:15px">📍 Miasta biur — nowe oferty sprzedaży dziś (wczoraj)</h3>
<table style="border-collapse:collapse;margin:0 0 16px;font-size:13px"><thead><tr style="background:#f3f4f6"><th style="padding:5px 10px;text-align:left">Miasto</th><th style="padding:5px 10px;text-align:right">🏢 Mieszkania</th><th style="padding:5px 10px;text-align:right">🏠 Domy</th><th style="padding:5px 10px;text-align:right">🌳 Działki</th><th style="padding:5px 10px;text-align:right">🏪 Lokale</th></tr></thead><tbody>
{"".join("<tr><td style='padding:5px 10px'><b>" + cn + "</b></td>" + "".join(f"<td style='padding:5px 10px;text-align:right;color:{'#b45309' if t == 0 else '#111'}'><b>{t}</b> <span style='color:#888'>({y})</span></td>" for t, y in per) + "</tr>" for cn, per in city_rows)}
</tbody></table>
{('<div style="background:#fee2e2;border:2px solid #dc2626;border-radius:8px;padding:12px 14px;margin:0 0 14px;color:#7f1d1d"><b style="font-size:15px">🔴 ALARM' + (' — WYCENA PDF NIE DZIAŁA' if smoke_fail else ' — zerowy import mimo „sukcesu” joba') + ':</b><br>' + '<br>'.join(critical) + '</div>') if critical else ''}
{('<div style="background:#fff3cd;border:1px solid #ffe08a;border-radius:8px;padding:10px 14px;margin:0 0 14px"><b>Do sprawdzenia:</b><br>' + '<br>'.join(problems) + '</div>') if problems else ''}
<table style="border-collapse:collapse;font-size:14px;width:100%"><thead><tr style="background:#f3f4f6;text-align:left">
<th style="padding:6px 10px">Portal</th><th style="padding:6px 10px">Strona portalu</th><th style="padding:6px 10px;text-align:right">Dziś sprzedaż</th><th style="padding:6px 10px;text-align:right">Dziś wynajem</th><th style="padding:6px 10px;text-align:right">Wczoraj sprz.</th><th style="padding:6px 10px;text-align:right">Wczoraj wyn.</th><th style="padding:6px 10px">Ostatni bieg</th></tr></thead>
<tbody>{tr}</tbody></table>
<p style="color:#888;font-size:12px;margin-top:14px">🔴 = job skończył się dziś sukcesem, ale wczytał 0 ofert (cicha awaria parsera/blokada) · ✅ = portal odpowiada, workflow OK, są dzisiejsze oferty sprzedaży i wynajmu · ⚠️ = portal odpowiada, ale import sprzedaży lub wynajmu nie dał ofert albo workflow padł · ❌ = portal nie odpowiada (blokada/awaria).<br>
Workflowy: https://github.com/{os.environ.get('GITHUB_REPOSITORY','')}/actions</p></div>"""

    print(subject); [print(r) for r in rows]; print("critical:", critical); print("problems:", problems)
    cfg = ((db.settings.find_one({"_id": "email"}) or {}).get("smtp") or {})
    if not (cfg.get("host") and cfg.get("user") and cfg.get("password")):
        print("Brak konfiguracji SMTP w settings.email — e-mail pominięty"); return
    to = os.environ.get("ALERT_EMAIL") or "pkukla35@gmail.com"
    msg = MIMEMultipart("alternative"); msg["Subject"] = subject
    msg["From"] = f"FinderDom.pl <{cfg.get('from_email') or cfg['user']}>"; msg["To"] = to
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", html), "plain", "utf-8")); msg.attach(MIMEText(html, "html", "utf-8"))
    port = int(cfg.get("port") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(cfg["host"], port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(cfg["user"], cfg["password"]); s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(cfg["user"], cfg["password"]); s.send_message(msg)
    print("e-mail wysłany do", to)


if __name__ == "__main__":
    main()
