#!/usr/bin/env python3
"""Generator artykułów bloga FinderDom.pl — wspólny template + treść per artykuł."""
import os

TEMPLATE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | FinderDom.pl</title>
<meta name="description" content="{description}">
<meta name="keywords" content="{keywords}">
<link rel="canonical" href="https://www.finderdom.pl/blog/{slug}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://www.finderdom.pl/blog/{slug}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/scripts/blog.css">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-9328001602201523" crossorigin="anonymous"></script>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"Article","headline":"{title}","author":{{"@type":"Person","name":"Paweł Kukla","url":"https://www.finderdom.pl/o-nas"}},"publisher":{{"@type":"Organization","name":"FinderDom.pl","logo":{{"@type":"ImageObject","url":"https://www.finderdom.pl/favicon.svg"}}}},"datePublished":"{date_iso}","dateModified":"{date_modified}","mainEntityOfPage":"https://www.finderdom.pl/blog/{slug}","image":"https://www.finderdom.pl/favicon.svg","description":"{description}"}}
</script>
{extra_schema}
</head>
<body>
<nav class="topnav">
  <div class="nav-inner">
    <a href="/" class="logo">🏠 FinderDom.pl</a>
    <div class="nav-links">
      <a href="/szukaj">Wyszukiwarka</a>
      <a href="/wycena">💰 Wyceń nieruchomość</a>
      <a href="/blog" class="active">📝 Blog</a>
      <a href="/pomoc">💬 Pomoc</a>
      <span id="authWidget" style="display:inline-flex;gap:8px;align-items:center;margin-left:auto"></span>
    </div>
  </div>
</nav>

<main>
  <div class="breadcrumb"><a href="/">Strona główna</a> · <a href="/blog">Blog</a> · {breadcrumb}</div>
  <h1>{title}</h1>
  <div class="lead">{lead}</div>
  <div class="meta">📅 {date_pl} · ⏱️ {read_time} · ✍️ <a href="/o-nas" style="color:inherit">Paweł Kukla, redakcja FinderDom.pl</a></div>

  {content}

  <div class="author-box">
    <div class="author-avatar">PK</div>
    <div>
      <div class="author-name">Paweł Kukla</div>
      <div class="author-bio">Założyciel FinderDom.pl — wyszukiwarki agregującej ogłoszenia nieruchomości z największych polskich portali. Na co dzień analizuje ceny z kilkunastu tysięcy aktualnych ofert i opisuje rynek bez marketingowej otoczki. <a href="/o-nas">Więcej o redakcji →</a></div>
    </div>
  </div>

  <div class="cta-box">
    <h3>{cta_title}</h3>
    <p>{cta_desc}</p>
    <a href="{cta_url}" class="btn">{cta_button}</a>
  </div>

  <div class="related">
    <div class="related-title">Może Cię zainteresować</div>
    <div class="related-grid">
      {related}
    </div>
  </div>

  <div class="fd-ad-slot" data-slot="article-bottom" data-format="horizontal"></div>
</main>

<footer>
  <div>© 2026 FinderDom.pl</div>
  <div style="display:flex;flex-wrap:wrap;gap:16px">
    <a href="/">Strona główna</a>
    <a href="/blog">Blog</a>
    <a href="/pomoc">Pomoc</a>
    <a href="/regulamin">Regulamin</a>
    <a href="/polityka-prywatnosci">Polityka prywatności</a>
    <a href="mailto:kontakt@finderdom.pl">Kontakt</a>
  </div>
</footer>

<script src="/scripts/ads.js" defer></script>
<script>
  (function renderAuthWidget(){{
    const el = document.getElementById('authWidget');
    if (!el) return;
    const token = localStorage.getItem('finderdom_token');
    if (token) el.innerHTML = '<a href="/konto" class="a-cta">👤 Moje konto</a>';
    else el.innerHTML = '<a href="/logowanie" class="a-login">Zaloguj</a><a href="/rejestracja" class="a-cta">Zarejestruj</a>';
  }})();
</script>
</body>
</html>
"""

ALL_POSTS = [
    {"slug": "ceny-mieszkan-w-polsce-2026-ranking-miast", "icon": "🏙️", "title": "Ceny mieszkań w czerwcu 2026: ranking 20 miast z ofert", "desc": "Ranking 20 miast wg mediany zł/m² z ok. 10 039 ofert FinderDom.pl: od Warszawy ("},
    {"slug": "rynek-pierwotny-czy-wtorny-2026", "icon": "🏗️", "title": "Rynek pierwotny czy wtórny w 2026? Ceny w 20 miastach", "desc": "Co opłaca się bardziej w 2026: mieszkanie od dewelopera czy z rynku wtórnego? Po"},
    {"slug": "ile-kosztuje-mieszkanie-2-pokojowe-2026", "icon": "🛏️", "title": "Ile kosztuje kawalerka i mieszkanie 2-, 3-, 4-pokojowe w 2026?", "desc": "Mediany cen kawalerek oraz mieszkań 2-, 3- i 4+ pokojowych w 20 miastach w 2026 "},
    {"slug": "koszty-zakupu-mieszkania-pcc-notariusz-2026", "icon": "🧾", "title": "Koszty zakupu mieszkania 2026: PCC, notariusz, KW, pośrednik", "desc": "Ile naprawdę kosztuje kupno mieszkania w 2026? PCC 2%, taksa notarialna z VAT, o"},
    {"slug": "ksiega-wieczysta-jak-sprawdzic-mieszkanie", "icon": "📜", "title": "Księga wieczysta mieszkania: jak sprawdzić KW przed zakupem", "desc": "Jak sprawdzić księgę wieczystą przed zakupem mieszkania krok po kroku: skąd wzią"},
    {"slug": "umowa-przedwstepna-zadatek-zaliczka", "icon": "✍️", "title": "Umowa przedwstępna kupna mieszkania: zadatek, forma, zapisy", "desc": "Umowa przedwstępna zakupu mieszkania: zadatek czy zaliczka, umowa zwykła czy not"},
    {"slug": "odbior-mieszkania-od-dewelopera-checklista", "icon": "🔧", "title": "Odbiór mieszkania od dewelopera: checklista 50 punktów i prawa", "desc": "Jak odebrać mieszkanie od dewelopera: narzędzia, normy odchyłek, checklista tech"},
    {"slug": "jak-negocjowac-cene-mieszkania", "icon": "🤝", "title": "Jak negocjować cenę mieszkania w 2026: 12 technik i realny rabat", "desc": "Ile realnie urwiesz z ceny mieszkania w 2026? 12 technik negocjacji, dane o czas"},
    {"slug": "czerwone-flagi-w-ogloszeniach-nieruchomosci", "icon": "🚩", "title": "Czerwone flagi w ogłoszeniach nieruchomości: jak nie dać się oszukać", "desc": "Jak rozpoznać oszustwo, zawyżoną cenę i ukryte wady w ogłoszeniu o mieszkaniu? P"},
    {"slug": "podatek-od-sprzedazy-nieruchomosci-przed-uplywem-5-lat", "icon": "🏛️", "title": "Podatek od sprzedaży mieszkania przed 5 latami – jak go uniknąć", "desc": "Sprzedajesz mieszkanie przed upływem 5 lat? Sprawdź, jak liczyć termin, jaki jes"},
    {"slug": "wynajem-mieszkania-umowa-najmu-okazjonalnego", "icon": "🔑", "title": "Wynajem mieszkania 2026: najem okazjonalny, zwykły i kaucja", "desc": "Najem zwykły, okazjonalny i instytucjonalny w 2026: różnice w ochronie lokatorów"},
    {"slug": "flipping-mieszkan-czy-sie-oplaca-2026", "icon": "📈", "title": "Flipping mieszkań w 2026: czy się opłaca? Kalkulacja i podatki", "desc": "Flipping mieszkań w 2026 roku: model biznesowy, pełna kalkulacja flipu 48 m² w Ł"},
    {"slug": "rynek-mieszkan-warszawa-2026", "icon": "📈", "title": "Rynek mieszkań w Warszawie 2026 — analiza cen, dzielnic i prognoz", "desc": "Rynek mieszkań w Warszawie 2026 — analiza cen"},
    {"slug": "wycena-nieruchomosci-metody", "icon": "💰", "title": "Wycena nieruchomości — 3 metody profesjonalnej wyceny", "desc": "Metoda porównawcza, dochodowa, kosztowa"},
    {"slug": "dom-czy-mieszkanie", "icon": "🏡", "title": "Dom czy mieszkanie? Co się bardziej opłaca w 2026", "desc": "Porównanie kosztów i opłacalności"},
    {"slug": "kredyt-hipoteczny-2026", "icon": "🏦", "title": "Kredyt hipoteczny 2026 — warunki, WIBOR, oprocentowanie", "desc": "Warunki i oprocentowanie kredytu w 2026"},
    {"slug": "mpzp-warunki-zabudowy", "icon": "📋", "title": "MPZP i warunki zabudowy — co musisz wiedzieć", "desc": "Miejscowy plan zagospodarowania i decyzja WZ"},
    {"slug": "sprzedaz-mieszkania-poradnik", "icon": "🎯", "title": "Jak sprzedać mieszkanie szybko — 10 sprawdzonych sposobów", "desc": "Home staging, cena, marketing zdjęć"},
    {"slug": "inwestycje-nieruchomosci-2026", "icon": "💼", "title": "Inwestowanie w nieruchomości 2026 — najlepsze dzielnice w Polsce", "desc": "Ranking dzielnic pod wynajem"},
    {"slug": "cena-metra-nieruchomosci", "icon": "📊", "title": "Cena za metr kwadratowy — jak ją interpretować", "desc": "Co oznacza cena/m² i czemu porównywanie ofert bywa mylące"},
]


def related_for(current_slug, all_posts, other_slug="jak-kupic-dzialke-budowlana"):
    """Zwróć HTML z 4 innymi postami."""
    items = [p for p in all_posts if p["slug"] != current_slug]
    # dodaj pierwszy post (poza generowanymi) jeśli nie ma
    if not any(p["slug"] == other_slug for p in items):
        items = [{"slug": other_slug, "icon": "🏗️",
                  "title": "Jak kupić działkę budowlaną",
                  "desc": "Kompletny poradnik krok po kroku"}] + items
    items = items[:4]
    html = ""
    for it in items:
        html += f"""      <a href="/blog/{it['slug']}" class="related-card">
        <div class="related-card-title">{it['icon']} {it['title'][:60]}</div>
        <div class="related-card-desc">{it['desc']}</div>
      </a>\n"""
    return html


def generate(slug: str, meta: dict, content: str):
    meta = dict(meta)
    meta.setdefault("extra_schema", "")
    meta.setdefault("date_modified", meta.get("date_iso", ""))
    related_html = related_for(slug, ALL_POSTS)
    html = TEMPLATE.format(
        slug=slug, related=related_html, content=content, **meta
    )
    out = f"/app/finderdom-landing/blog/{slug}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✔ {out}")


if __name__ == "__main__":
    # Placeholder — konkretne artykuły są dodawane w osobnych plikach content_*.py
    pass
