#!/usr/bin/env python3
"""Po wygenerowaniu artykułów: dodaj karty do blog.html, URL-e do sitemap.xml, wpisy do gen_blog.ALL_POSTS."""
import json, re, html

ROOT = "/app/finderdom-landing"
meta = json.load(open("/tmp/new_posts_meta.json"))
posts = list(meta.values())

# 1) blog.html — karty na początku gridu
cards = ""
for p in posts:
    cards += f'''    <a href="/blog/{p['slug']}" class="card">
      <div class="card-icon">{p['icon']}</div>
      <div class="card-tag">{p['tag']}</div>
      <div class="card-title">{html.escape(p['title'])}</div>
      <div class="card-desc">{html.escape(p['desc'])}</div>
      <div class="card-meta"><span>📅 {p['date_pl']}</span><span class="card-read">Czytaj →</span></div>
    </a>

'''
b = open(f"{ROOT}/blog.html", encoding="utf-8").read()
# usuń poprzednio wstawione karty (idempotentnie)
b = re.sub(r"<!-- NEW-POSTS-START -->.*?<!-- NEW-POSTS-END -->\n", "", b, flags=re.S)
b = b.replace('  <div class="grid">\n', f'  <div class="grid">\n<!-- NEW-POSTS-START -->\n{cards}<!-- NEW-POSTS-END -->\n', 1)
open(f"{ROOT}/blog.html", "w", encoding="utf-8").write(b)

# 2) sitemap.xml
sm = open(f"{ROOT}/sitemap.xml", encoding="utf-8").read()
for p in posts:
    loc = f"https://www.finderdom.pl/blog/{p['slug']}"
    if loc in sm:
        continue
    entry = f'  <url><loc>{loc}</loc><lastmod>{p["date_iso"]}</lastmod><changefreq>monthly</changefreq><priority>0.8</priority></url>\n'
    sm = sm.replace("</urlset>", entry + "</urlset>")
open(f"{ROOT}/sitemap.xml", "w", encoding="utf-8").write(sm)

# 3) gen_blog.ALL_POSTS (do sekcji "Może Cię zainteresować" przy kolejnych generacjach)
g = open(f"{ROOT}/scripts/gen_blog.py", encoding="utf-8").read()
add = ""
for p in posts:
    if f'"slug": "{p["slug"]}"' in g:
        continue
    add += f'    {{"slug": "{p["slug"]}", "icon": "{p["icon"]}", "title": {json.dumps(p["title"], ensure_ascii=False)}, "desc": {json.dumps(p["desc"][:80], ensure_ascii=False)}}},\n'
if add:
    g = g.replace("ALL_POSTS = [\n", "ALL_POSTS = [\n" + add, 1)
    open(f"{ROOT}/scripts/gen_blog.py", "w", encoding="utf-8").write(g)

print(f"OK: {len(posts)} postów → blog.html, sitemap.xml, gen_blog.ALL_POSTS")
