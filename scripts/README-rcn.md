# RCN — Rejestr Cen Nieruchomości

Skrypt pobierający **darmowe** dane transakcyjne z rejestrów cen nieruchomości (GUGiK) i wrzucający je do MongoDB.

## Źródło danych

- **Rejestr Cen Nieruchomości (RCN)** — dane z aktów notarialnych, prowadzone przez starostów
- Bezpłatny od **13 lutego 2026** (ustawa z 26 września 2025 r.)
- **362 powiaty** dostępne (stan sierpień 2026)
- Endpoint WFS: `https://mapy.geoportal.gov.pl/wss/service/rcn`
- Atrybuty: `cena_brutto`, `powierzchnia`, `data_zawarcia`, `rodzaj_nieruchomosci`, `rodzaj_rynku`, `TERYT`

## Uruchamianie

### Wymagania
```bash
pip install -r scripts/requirements.txt
export MONGO_URL="mongodb+srv://..."   # lub w backend/.env
```

### Komendy
```bash
# Wszystkie 20 miast (pełny import — może potrwać 10-30 min)
python scripts/rcn_ingest.py

# Tylko jedno miasto
python scripts/rcn_ingest.py --city Warszawa

# Tylko transakcje od danej daty
python scripts/rcn_ingest.py --since 2024-01-01

# Test — tylko wykryj dostępne warstwy WFS
python scripts/rcn_ingest.py --probe
```

### Cron (miesięczna aktualizacja)
Zalecane odpalać raz w miesiącu (dane RCN odświeżają się co ~2 tyg.):
```
0 3 1 * * cd /path/to/finderdom-landing && python scripts/rcn_ingest.py --since 2024-01-01
```

Albo GitHub Actions (patrz `.github/workflows/rcn-refresh.yml`, do zrobienia).

## Struktura MongoDB

Kolekcja `rcn_transactions`:
```json
{
  "city": "Warszawa",
  "rodzaj_nieruchomosci": "lokal",         // lokal | budynek | dzialka
  "cena_brutto": 850000.00,
  "powierzchnia": 55.5,
  "cena_m2": 15315.32,
  "data_zawarcia": "2024-08-15",
  "rodzaj_rynku": "wtórny",                // pierwotny | wtórny
  "teryt": "1465",
  "raw_keys": ["cena_brutto", "powierzchnia", ...],  // debug
  "ingested_at": ISODate("2026-06-10T14:32:00Z")
}
```

Unikalny compound index: `(teryt, data_zawarcia, cena_brutto, powierzchnia)` — pozwala na re-uruchomienie skryptu bez duplikatów.

## Wykorzystanie w wycenie PDF

Wycena PDF automatycznie pobiera medianę cen/m² z tej kolekcji przy generowaniu raportu:
- **Filtry**: `city` + `rodzaj_nieruchomosci` + `powierzchnia ±25%` + ostatnie 3 lata
- Min. **5 transakcji** — poniżej używa fallback (94% wywoławczej)
- Endpoint publiczny: `GET /api/rcn/stats?city=Warszawa&type=mieszkanie&area=55`

Sekcja PDF "Realna cena transakcyjna" pokazuje:
- Jeśli dane RCN dostępne (≥5 transakcji): `"RCN — N aktów notarialnych"`
- W przeciwnym razie: `"Szacowana cena transakcyjna sprzedaży"` (jak wcześniej)

## Testowanie

```bash
# Sprawdź czy WFS działa i wykrywa warstwy
python scripts/rcn_ingest.py --probe

# Sprawdź statystyki z konkretnego miasta
curl "https://finderdom.pl/api/rcn/stats?city=Warszawa&type=mieszkanie&area=55"
```

## Legalność

- Dane RCN są **publiczne i bezpłatne** od 13.02.2026 (ustawa z 26.09.2025 r.)
- GUGiK aktywnie zachęca do integracji: [https://www.geoportal.gov.pl/pl/dane/rejestr-cen-nieruchomosci-rcn/](https://www.geoportal.gov.pl/pl/dane/rejestr-cen-nieruchomosci-rcn/)
- Nie wymaga API key, nie ma limitu żądań (poza fair use)
