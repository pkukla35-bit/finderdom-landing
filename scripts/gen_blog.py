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
<link rel="canonical" href="https://finderdom.pl/blog/{slug}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://finderdom.pl/blog/{slug}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/scripts/blog.css">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7963354786615192" crossorigin="anonymous"></script>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"Article","headline":"{title}","author":{{"@type":"Organization","name":"FinderDom.pl"}},"publisher":{{"@type":"Organization","name":"FinderDom.pl","logo":{{"@type":"ImageObject","url":"https://finderdom.pl/favicon.svg"}}}},"datePublished":"{date_iso}","dateModified":"{date_iso}","mainEntityOfPage":"https://finderdom.pl/blog/{slug}","image":"https://finderdom.pl/favicon.svg","description":"{description}"}}
</script>
</head>
<body>
<nav class="topnav">
  <div class="nav-inner">
    <a href="/" class="logo">🏠 FinderDom.pl</a>
    <div class="nav-links">
      <a href="/szukaj.html">Wyszukiwarka</a>
      <a href="/wycena.html">💰 Wyceń nieruchomość</a>
      <a href="/blog" class="active">📝 Blog</a>
      <a href="/pomoc.html">💬 Pomoc</a>
      <span id="authWidget" style="display:inline-flex;gap:8px;align-items:center;margin-left:auto"></span>
    </div>
  </div>
</nav>

<main>
  <div class="breadcrumb"><a href="/">Strona główna</a> · <a href="/blog">Blog</a> · {breadcrumb}</div>
  <h1>{title}</h1>
  <div class="lead">{lead}</div>
  <div class="meta">📅 {date_pl} · ⏱️ {read_time} · 📝 FinderDom.pl</div>

  {content}

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
    <a href="/pomoc.html">Pomoc</a>
    <a href="/regulamin">Regulamin</a>
    <a href="/polityka-prywatnosci">Polityka prywatności</a>
    <a href="mailto:kontakt@finderdom.pl">Kontakt</a>
  </div>
</footer>

<script src="/scripts/ads.js"></script>
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
