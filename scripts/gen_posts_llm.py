#!/usr/bin/env python3
"""
Generator 12 nowych artykułów eksperckich bloga FinderDom.pl (czerwiec 2026).

Treść pisze LLM (Claude Sonnet) na podstawie szczegółowego briefu + REALNYCH statystyk
z bazy FinderDom (/tmp/city_stats.json — mediany cen z ~14 000 aktualnych ofert).
Każdy artykuł: 1500–2000 słów, tabele, callouty, FAQ (+ FAQPage schema), autor, źródła.

Uruchomienie (klucz z /app/backend/.env):
    python3 scripts/gen_posts_llm.py            # wszystkie
    python3 scripts/gen_posts_llm.py slug1 slug2  # wybrane
"""
import asyncio, json, os, re, sys, html as _html
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from emergentintegrations.llm.chat import LlmChat, UserMessage
import gen_blog

KEY = os.environ["EMERGENT_LLM_KEY"]
STATS = json.load(open("/tmp/city_stats.json")) if os.path.exists("/tmp/city_stats.json") else {}
OUT_META = Path("/tmp/new_posts_meta.json")

DATE_ISO = "2026-06-14"
DATE_PL = "14 czerwca 2026"


def stats_table(kind: str) -> str:
    """Gotowa tabela HTML z realnych danych (LLM ma ją wstawić w artykule bez zmian)."""
    rows = sorted(STATS.items(), key=lambda kv: -(kv[1]["med_pm2"] or 0))
    fmt = lambda v: f"{v:,}".replace(",", " ") if v else "—"
    if kind == "pm2":
        tr = "".join(f"<tr><td>{c}</td><td>{fmt(s['med_pm2'])} zł</td><td>{fmt(s['p25'])}–{fmt(s['p75'])} zł</td><td>{fmt(s['med_price'])} zł</td><td>{s['med_area']} m²</td></tr>" for c, s in rows)
        return f"<table><tr><th>Miasto</th><th>Mediana zł/m²</th><th>Typowy przedział (25–75%)</th><th>Mediana ceny</th><th>Mediana metrażu</th></tr>{tr}</table>"
    if kind == "market":
        tr = "".join(f"<tr><td>{c}</td><td>{fmt(s['pierwotny'])} zł</td><td>{fmt(s['wtorny'])} zł</td><td>{(round((s['pierwotny']/s['wtorny']-1)*100) if s['pierwotny'] and s['wtorny'] else 0):+d}%</td></tr>" for c, s in rows)
        return f"<table><tr><th>Miasto</th><th>Rynek pierwotny (mediana zł/m²)</th><th>Rynek wtórny (mediana zł/m²)</th><th>Premia za nowe</th></tr>{tr}</table>"
    if kind == "rooms":
        tr = "".join(f"<tr><td>{c}</td><td>{fmt(s['rooms'].get('1'))} zł</td><td>{fmt(s['rooms'].get('2'))} zł</td><td>{fmt(s['rooms'].get('3'))} zł</td><td>{fmt(s['rooms'].get('4'))} zł</td></tr>" for c, s in rows)
        return f"<table><tr><th>Miasto</th><th>Kawalerka</th><th>2 pokoje</th><th>3 pokoje</th><th>4+ pokoje</th></tr>{tr}</table>"
    return ""


TOTAL = sum(s["total_db"] for s in STATS.values()) if STATS else 14000

POSTS = [
    {"slug": "ceny-mieszkan-w-polsce-2026-ranking-miast", "icon": "🏙️", "tag": "Analiza rynku",
     "topic": "Ceny mieszkań w Polsce w czerwcu 2026 — ranking 20 miast na podstawie realnych ofert FinderDom.pl",
     "brief": "Artykuł analityczny. WSTAW DOKŁADNIE poniższą tabelę (bez zmian liczb) w sekcji 'Ranking miast'. Omów: najdroższe i najtańsze miasta, rozpiętość 25–75% (co mówi o zróżnicowaniu dzielnic), stosunek ceny do metrażu, co to oznacza dla kupującego z budżetem 500 tys. / 800 tys. zł, metodologia (mediana vs średnia, dlaczego mediana). Odnieś się do liczby ofert w bazie i zaznacz, że to ceny OFERTOWE, nie transakcyjne (zwykle 3–7% niżej po negocjacjach).",
     "data": stats_table("pm2")},
    {"slug": "rynek-pierwotny-czy-wtorny-2026", "icon": "🏗️", "tag": "Poradnik",
     "topic": "Rynek pierwotny czy wtórny w 2026 — co się bardziej opłaca? Porównanie cen w 20 miastach",
     "brief": "WSTAW DOKŁADNIE poniższą tabelę w sekcji z porównaniem cen. Omów: ukryte koszty rynku pierwotnego (wykończenie 1500–3000 zł/m², garaż, komórka, czas oczekiwania, PCC 0% ale VAT w cenie), koszty wtórnego (PCC 2%, remont, stan instalacji, wspólnota), ryzyka (upadłość dewelopera, rachunek powierniczy, umowa deweloperska vs rezerwacyjna, DFG), gdzie premia za nowe jest ujemna i dlaczego (np. stare kamienice w centrum vs nowe osiedla na obrzeżach). Checklista decyzyjna na koniec.",
     "data": stats_table("market")},
    {"slug": "ile-kosztuje-mieszkanie-2-pokojowe-2026", "icon": "🛏️", "tag": "Analiza rynku",
     "topic": "Ile kosztuje kawalerka, mieszkanie 2-, 3- i 4-pokojowe w 2026? Mediany cen w 20 miastach",
     "brief": "WSTAW DOKŁADNIE poniższą tabelę. Omów: różnica ceny między kolejnymi pokojami (ile kosztuje 'dodatkowy pokój'), gdzie kawalerki są relatywnie najdroższe za m² i dlaczego (inwestorzy pod wynajem), jaki metraż typowo kryje się za 2 i 3 pokojami, mieszkanie dla singla/pary/rodziny 2+2 — rekomendacje budżetowe, rata kredytu przy 20% wkładu i oprocentowaniu ok. 7% dla każdej kategorii (policz przykłady dla Warszawy, Krakowa, Łodzi).",
     "data": stats_table("rooms")},
    {"slug": "koszty-zakupu-mieszkania-pcc-notariusz-2026", "icon": "🧾", "tag": "Finanse",
     "topic": "Koszty zakupu mieszkania 2026 — PCC, notariusz, sąd wieczystoksięgowy, pośrednik, kredyt. Pełny kosztorys",
     "brief": "Pełny kosztorys 'okołozakupowy': PCC 2% (rynek wtórny; zwolnienie dla pierwszego mieszkania — warunki), taksa notarialna wg rozporządzenia (progi i maksymalne stawki + VAT 23%), wypisy aktu, opłaty sądowe (wpis własności 200 zł, hipoteka 200 zł, założenie KW), PCC od hipoteki 19 zł, prowizja pośrednika 1–3% + VAT, koszty kredytu (prowizja, wycena, ubezpieczenia), koszty na rynku pierwotnym. Policz 3 przykłady: mieszkanie 450 tys., 700 tys., 1,1 mln zł — tabela z sumami. Jak zaoszczędzić legalnie.",
     "data": ""},
    {"slug": "ksiega-wieczysta-jak-sprawdzic-mieszkanie", "icon": "📜", "tag": "Prawo",
     "topic": "Księga wieczysta — jak sprawdzić mieszkanie przed zakupem krok po kroku (działy I–IV, EKW, pułapki)",
     "brief": "Praktyczny przewodnik: czym jest KW, jak znaleźć numer (akt, pośrednik, geoportal — co legalne), jak czytać działy I-O, I-Sp, II, III, IV; wzmianki i co oznaczają; hipoteka — jak kupić mieszkanie z hipoteką bezpiecznie (promesa banku, list mazalny); służebności, roszczenia, ostrzeżenia o niezgodności; spółdzielcze własnościowe prawo bez KW — ryzyka; mieszkanie bez KW; różnica KW lokalu vs KW gruntowa budynku; czerwone flagi. Checklista 12 punktów.",
     "data": ""},
    {"slug": "umowa-przedwstepna-zadatek-zaliczka", "icon": "✍️", "tag": "Prawo",
     "topic": "Umowa przedwstępna zakupu mieszkania — zadatek czy zaliczka, forma, zapisy, które chronią kupującego",
     "brief": "Forma zwykła pisemna vs akt notarialny (skutek silniejszy — roszczenie o zawarcie umowy, roszczenie w KW), zadatek (art. 394 KC) vs zaliczka — różnice z przykładami kwotowymi, wysokość zadatku (zwyczajowo 5–10%), zapisy obowiązkowe (strony, przedmiot, cena, termin umowy przyrzeczonej, stan prawny, wydanie lokalu, meldunek, zaległości czynszowe, kary umowne), klauzula kredytowa (warunek uzyskania kredytu), co gdy sprzedający się wycofa, przedawnienie roszczeń (1 rok), najczęstsze błędy. Wzór struktury umowy w punktach.",
     "data": ""},
    {"slug": "odbior-mieszkania-od-dewelopera-checklista", "icon": "🔧", "tag": "Poradnik",
     "topic": "Odbiór mieszkania od dewelopera — checklista techniczna 50 punktów, protokół, usterki, prawa kupującego",
     "brief": "Przygotowanie (narzędzia: poziomica 2 m, kątownik, dalmierz, próbnik gniazdek, latarka, kreda), normy odchyłek (PN-B-10110 tynki: 3 mm na 2 m łacie; posadzki; piony), co sprawdzać pomieszczenie po pomieszczeniu (okna — przedmuchy, regulacja; drzwi; instalacje; wentylacja — test kartką; balkon — spadki, obróbki; metraż wg normy PN-ISO 9836 i rozliczenie różnicy), protokół odbioru i terminy z ustawy deweloperskiej 2022 (14 dni na stanowisko, 30 dni na usunięcie), odmowa odbioru, inżynier do odbioru (koszt 400–900 zł), rękojmia 5 lat. Checklista podzielona na sekcje.",
     "data": ""},
    {"slug": "jak-negocjowac-cene-mieszkania", "icon": "🤝", "tag": "Poradnik",
     "topic": "Jak negocjować cenę mieszkania w 2026 — 12 technik, argumenty z danych i ile realnie da się urwać",
     "brief": "Realne pole negocjacji (ofertowe vs transakcyjne 3–7%, więcej przy długo wiszących ofertach), jak sprawdzić czas ekspozycji oferty i historię obniżek (agregatory, FinderDom pokazuje datę dodania i porównanie do mediany dzielnicy), argumenty techniczne (stan instalacji, okna, piętro bez windy, ekspozycja, hałas), argumenty finansowe (gotówka, szybki termin, brak łańcucha), błędy kupujących (kotwiczenie na cenie ofertowej, emocje, ujawnianie budżetu), negocjacje z deweloperem (rabat na wykończenie, garaż, harmonogram), negocjacje z pośrednikiem prowizji, kiedy odpuścić. Scenariusze dialogów.",
     "data": ""},
    {"slug": "czerwone-flagi-w-ogloszeniach-nieruchomosci", "icon": "🚩", "tag": "Bezpieczeństwo",
     "topic": "Czerwone flagi w ogłoszeniach nieruchomości — jak rozpoznać oszustwo, zawyżoną cenę i ukryte wady",
     "brief": "Sygnały ostrzegawcze: cena mocno poniżej mediany dzielnicy (podaj jak porównać z medianą), brak zdjęć lub zdjęcia stockowe/renderowane jako 'stan faktyczny', zdjęcia tej samej oferty w wielu ogłoszeniach z różnymi cenami, 'kontakt tylko mailowo', prośba o zaliczkę przed obejrzeniem (oszustwa na wynajem), brak numeru KW, 'udział w nieruchomości' zamiast lokalu, lokal 'do przekształcenia', mieszkanie w budynku bez odbioru, TBS/SIM sprzedawane jako własność, działka 'budowlana' bez MPZP/WZ, metraż z balkonem wliczony, piętro '0' = suterena, 'cicha okolica' przy drodze krajowej. Jak weryfikować w 15 minut (KW, geoportal, Street View, UM). Jak agregator pomaga wykryć duplikaty i zmiany cen.",
     "data": ""},
    {"slug": "podatek-od-sprzedazy-nieruchomosci-przed-uplywem-5-lat", "icon": "🏛️", "tag": "Finanse",
     "topic": "Podatek od sprzedaży mieszkania przed upływem 5 lat — PIT-39, ulga mieszkaniowa, jak legalnie go uniknąć",
     "brief": "Zasada 5 lat liczonych od końca roku nabycia (przykłady dat), stawka 19% od dochodu (przychód minus koszty nabycia i nakłady — co można zaliczyć, udokumentowanie remontów fakturami), ulga mieszkaniowa (wydatki na własne cele mieszkaniowe w 3 lata — katalog: zakup, spłata kredytu, remont, działka; wzór na zwolnioną część), PIT-39 do 30 kwietnia, spadek i darowizna (liczenie od nabycia przez spadkodawcę; koszty), sprzedaż po rozwodzie, mieszkanie z kredytem — spłata hipoteki jako cel mieszkaniowy (orzecznictwo), typowe błędy. 3 policzone przykłady w tabeli.",
     "data": ""},
    {"slug": "wynajem-mieszkania-umowa-najmu-okazjonalnego", "icon": "🔑", "tag": "Wynajem",
     "topic": "Wynajem mieszkania 2026 — najem okazjonalny vs zwykły, umowa, kaucja, podatek ryczałtowy, eksmisja",
     "brief": "Dla właścicieli i najemców: najem zwykły vs okazjonalny vs instytucjonalny (różnice w ochronie lokatorów, wymogi: oświadczenie notarialne o poddaniu się egzekucji, wskazanie lokalu zastępczego, zgłoszenie do US w 14 dni), kaucja (max 6-krotność/12-krotność, zwrot w 1 miesiąc, waloryzacja), umowa — zapisy obowiązkowe i zakazane (klauzule abuzywne), protokół zdawczo-odbiorczy, podwyżki czynszu (art. 8a ustawy o ochronie praw lokatorów — 3 miesiące wypowiedzenia), rozliczanie mediów, podatek: ryczałt 8,5% do 100 tys. i 12,5% powyżej (PIT-28), najem prywatny vs działalność, rentowność — policz na przykładzie Warszawy i Łodzi z realnych cen (podaj mediany: Warszawa 2 pokoje ok. 747 tys. zł, Łódź 2 pokoje ok. 375 tys. zł; czynsze rynkowe szacunkowo).",
     "data": ""},
    {"slug": "flipping-mieszkan-czy-sie-oplaca-2026", "icon": "📈", "tag": "Inwestycje",
     "topic": "Flipping mieszkań w 2026 — czy się jeszcze opłaca? Kalkulacja, podatki, ryzyka i gdzie szukać okazji",
     "brief": "Model biznesowy flipu, pełna kalkulacja na przykładzie: zakup 2-pokojowego 48 m² w Łodzi za ok. 330 tys. (poniżej mediany 375 tys.), remont 1800 zł/m², koszty transakcyjne (PCC, notariusz), koszt pieniądza (kredyt/gotówka), sprzedaż po 6 miesiącach, podatek 19% (PIT-39 lub działalność) — tabela z marżą netto. Gdzie szukać okazji (oferty długo na rynku, spadki cen, licytacje komornicze — ryzyka, spadkobiercy, mieszkania do generalnego remontu), jak agregator z historią cen pomaga, ryzyka 2026 (podaż nowych mieszkań, koszty robocizny, czas sprzedaży), alternatywa: buy-and-hold. Uczciwa ocena: kiedy NIE robić flipu.",
     "data": ""},
]

SYSTEM = (
    "Jesteś doświadczonym polskim analitykiem rynku nieruchomości i redaktorem portalu FinderDom.pl. "
    "Piszesz po polsku, konkretnie, bez lania wody i bez marketingowych frazesów. Używasz liczb, przykładów, tabel. "
    "Nie wymyślasz nieistniejących przepisów — jeśli nie masz pewności co do aktualnej stawki/progów, pisz 'ok.' i odsyłaj do źródła. "
    "Zwracasz WYŁĄCZNIE poprawny JSON (bez markdown, bez komentarzy)."
)

PROMPT1 = """Piszemy artykuł ekspercki na blog FinderDom.pl W DWÓCH CZĘŚCIACH (to jest CZĘŚĆ 1).

TEMAT: {topic}
BRIEF (co MUSI się znaleźć w całym artykule): {brief}
{data_block}
WYMAGANIA DLA CZĘŚCI 1:
- Zaplanuj cały artykuł: 7 sekcji <h2> (outline). W tej odpowiedzi napisz TYLKO pierwsze 3 sekcje (900–1000 słów — rozwijaj akapity, dawaj przykłady liczbowe) w polu content_html_part1.
- HTML: <h2>, <h3>, <p>, <ul>/<ol>, <table> (z <th>), <div class="callout"><div class="callout-title">…</div><p>…</p></div>. BEZ <h1>. Nagłówki <h2> zaczynaj od jednego emoji.
- Jeśli dostałeś gotową tabelę z danych — wstaw ją 1:1 w części 1.
- Pisz do kupującego/sprzedającego w drugiej osobie, konkretnie, z liczbami. Odnoś się do danych FinderDom.pl (baza ok. {total} aktualnych ofert mieszkań z Otodom, OLX, Morizon, Domiporta).

Zwróć JSON:
{{
 "title": "tytuł SEO do 70 znaków (bez '| FinderDom')",
 "description": "meta description 140–160 znaków",
 "keywords": "6–8 fraz po przecinku",
 "lead": "2–3 zdania wstępu — bez HTML",
 "read_time": "np. 11 min czytania",
 "outline": ["h2 sekcji 1", "...", "h2 sekcji 7"],
 "content_html_part1": "…"
}}"""

PROMPT2 = """Kontynuujemy artykuł na blog FinderDom.pl (CZĘŚĆ 2 z 2).

TYTUŁ: {title}
BRIEF: {brief}
OUTLINE (7 sekcji): {outline}
CZĘŚĆ 1 (już napisana, NIE powtarzaj jej treści):
{part1}

WYMAGANIA DLA CZĘŚCI 2:
- Napisz sekcje 4–7 z outline (900–1100 słów — rozwijaj akapity, dawaj przykłady liczbowe) w polu content_html_part2. Ten sam HTML i styl co w części 1. Min. 1 tabela i 1 callout. BEZ <h1>.
- W 1–2 miejscach naturalnie zaproponuj narzędzie FinderDom: wyszukiwarka <a href="/szukaj.html">/szukaj.html</a> (filtry: rynek pierwotny/wtórny, rok budowy, cena/m²) lub wycena <a href="/wycena.html">/wycena.html</a>.
- Ostatnia sekcja = praktyczne podsumowanie/checklista.
- Dodaj 5 pytań FAQ (konkretne odpowiedzi 2–4 zdania) oraz 3–5 źródeł (akty prawne / instytucje; bez wymyślonych URL — tylko domeny typu gov.pl, nbp.pl, gus.gov.pl, ekw.ms.gov.pl, podatki.gov.pl).

Zwróć JSON:
{{
 "content_html_part2": "…",
 "faq": [{{"q": "...", "a": "..."}}],
 "sources": ["...", "..."]
}}"""


def build_content(post: dict, d: dict) -> str:
    faq_html = "".join(f"<details><summary>{_html.escape(f['q'])}</summary><p>{_html.escape(f['a'])}</p></details>" for f in d.get("faq", []))
    src_html = "".join(f"<li>{_html.escape(s)}</li>" for s in d.get("sources", []))
    return (
        d["content_html"]
        + '\n<div class="fd-ad-slot" data-slot="article-mid" data-format="horizontal"></div>\n'
        + f'<section class="faq"><h2>❓ Najczęstsze pytania</h2>{faq_html}</section>\n'
        + (f'<div class="sources"><strong>Źródła i podstawa prawna:</strong><ul>{src_html}</ul></div>' if src_html else "")
    )


def faq_schema(d: dict) -> str:
    items = [{"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in d.get("faq", [])]
    if not items:
        return ""
    return '<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": items}, ensure_ascii=False) + "</script>"


def word_count(html_s: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html_s).split())


def _parse(txt: str) -> dict:
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S)
    start, end = txt.find("{"), txt.rfind("}")
    return json.loads(txt[start:end + 1])


async def _ask(session: str, prompt: str) -> dict:
    chat = LlmChat(api_key=KEY, session_id=session, system_message=SYSTEM).with_model("anthropic", "claude-sonnet-5-5").with_params(max_tokens=24000, extra_body={"output_config": {"effort": "low"}})
    return _parse(await chat.send_message(UserMessage(text=prompt)))


async def gen_one(post: dict) -> dict:
    """Dwa krótsze wywołania (proxy ucina odpowiedzi >~120 s)."""
    data_block = f"\nGOTOWA TABELA Z REALNYCH DANYCH FinderDom.pl (wstaw 1:1, nie zmieniaj liczb):\n{post['data']}\n" if post["data"] else ""
    total = f"{TOTAL:,}".replace(",", " ")
    p1 = await _ask(f"blog-{post['slug']}-p1", PROMPT1.format(topic=post["topic"], brief=post["brief"], data_block=data_block, total=total))
    p2 = await _ask(f"blog-{post['slug']}-p2", PROMPT2.format(title=p1["title"], brief=post["brief"], outline=json.dumps(p1.get("outline", []), ensure_ascii=False), part1=p1["content_html_part1"]))
    d = {**p1, **p2, "content_html": p1["content_html_part1"] + "\n" + p2["content_html_part2"]}
    d["_words"] = word_count(d["content_html"])
    if d["_words"] < 1100:
        print(f"  ⚠ {post['slug']}: tylko {d['_words']} słów")
    return d


async def main(only: list[str]):
    meta_all = json.load(open(OUT_META)) if OUT_META.exists() else {}
    for post in POSTS:
        if only and post["slug"] not in only:
            continue
        print(f"▶ {post['slug']}")
        try:
            d = await gen_one(post)
        except Exception as e:
            print(f"  ✖ {post['slug']}: {e}")
            continue
        meta = {
            "title": d["title"], "description": d["description"], "keywords": d["keywords"],
            "breadcrumb": d["title"][:60], "date_iso": DATE_ISO, "date_pl": DATE_PL, "date_modified": DATE_ISO,
            "read_time": d.get("read_time", "10 min czytania"), "lead": d["lead"],
            "cta_title": "🔍 Sprawdź aktualne ceny w swoim mieście",
            "cta_desc": f"FinderDom.pl agreguje ok. {TOTAL:,} aktualnych ofert mieszkań z Otodom, OLX, Morizon i Domiporty. Filtruj po rynku, roku budowy i cenie za m² — w jednym miejscu.".replace(",", " "),
            "cta_url": "/szukaj.html", "cta_button": "Otwórz wyszukiwarkę →",
            "extra_schema": faq_schema(d),
        }
        gen_blog.generate(post["slug"], meta, build_content(post, d))
        meta_all[post["slug"]] = {"slug": post["slug"], "icon": post["icon"], "tag": post["tag"], "title": d["title"], "desc": d["description"], "date_pl": DATE_PL, "date_iso": DATE_ISO, "words": d["_words"]}
        OUT_META.write_text(json.dumps(meta_all, ensure_ascii=False, indent=1))
        print(f"  ✔ {d['_words']} słów")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
