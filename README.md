# 🏠 FinderDom.pl — Landing Page

Stronę „Coming Soon" dla wyszukiwarki ogłoszeń nieruchomości.

## 📁 Struktura

```
finderdom-landing/
├── index.html          # Strona główna (statyczna, PL, responsywna)
├── api/
│   └── subscribe.js    # Serverless function - zapis emaili
├── favicon.svg         # Ikona
├── robots.txt          # SEO
├── sitemap.xml         # SEO
├── vercel.json         # Konfiguracja Vercel
└── package.json        # Zależności (mongodb)
```

## 🚀 Deployment na Vercel — INSTRUKCJA KROK PO KROKU

### KROK 1 — Wypchnij pliki na GitHub

Opcja A (najprostsza): W terminalu Emergent poniżej masz gotowe do skopiowania komendy.

Opcja B: Ręcznie utwórz nowe repo `finderdom-landing` na GitHubie i wrzuć zawartość folderu `/app/finderdom-landing/`.

### KROK 2 — Import w Vercel

1. Wejdź na https://vercel.com/new
2. Kliknij **Import Git Repository**
3. Wybierz repo `finderdom-landing` (albo folder `finderdom-landing` z repo `taxigo` jeśli wrzucamy do istniejącego)
4. Framework Preset: **Other** (bez frameworka)
5. Root Directory: `finderdom-landing` (jeśli w tym samym repo co taxigo) LUB `./` (jeśli osobne repo)
6. **NIE klikaj jeszcze Deploy** — najpierw dodaj zmienne środowiskowe

### KROK 3 — Zmienne środowiskowe w Vercel

W sekcji **Environment Variables** dodaj:

| Nazwa | Wartość | Skąd wziąć |
|---|---|---|
| `MONGO_URL` | `mongodb+srv://...` | Z `/app/backend/.env` (to samo co TAXIGO) |
| `RESEND_API_KEY` | `re_...` | Z `/app/backend/.env` |
| `RESEND_FROM` | `FinderDom <noreply@twoja-domena>` | Skonfiguruj w Resend |
| `RESEND_OWNER_CC` | `pkukla35@gmail.com` | Twój email |

### KROK 4 — Deploy

Kliknij **Deploy**. Za ~1 minutę strona będzie online pod adresem `finderdom-landing.vercel.app`.

### KROK 5 — Podepnij domenę `finderdom.pl`

1. W Vercel: Settings → Domains → Add
2. Wpisz: `finderdom.pl` i `www.finderdom.pl`
3. Vercel pokaże wymagane rekordy DNS (przykład):
   ```
   A     @     76.76.21.21
   CNAME www   cname.vercel-dns.com
   ```
4. Idź na **Home.pl → Panel Klienta → Domeny → finderdom.pl → DNS**
5. Wpisz rekordy jak wyżej
6. Poczekaj 15-60 min na propagację (Vercel automatycznie wystawi SSL)

### KROK 6 — Test

Wejdź na `https://finderdom.pl` — strona powinna działać, formularz powinien zapisywać emaile w MongoDB (kolekcja `finderdom.landing_subscribers`) i wysyłać powiadomienie na Twój email.

## 📊 Podgląd zapisów

Wszystkie emaile trafiają do MongoDB:
- Baza: `finderdom`
- Kolekcja: `landing_subscribers`
- Pola: `{email, source, created_at, ip, user_agent, last_seen_at}`

Możesz je podejrzeć w MongoDB Atlas albo prostym endpointem admin (do dodania w kolejnej sesji).

## 🔧 Development lokalnie

```bash
cd /app/finderdom-landing
yarn install
# Testowanie z Vercel CLI:
npx vercel dev
```

## 📝 TODO na następne sesje

- [ ] Panel admin do przeglądu zapisów
- [ ] Google Analytics / Plausible
- [ ] OG image (opengraph.png 1200x630)
- [ ] Meta Pixel / Facebook Conversions API
- [ ] Refactor do Next.js 15 (gdy będziemy budować pełną aplikację)
