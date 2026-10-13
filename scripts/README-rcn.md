# RCN — Rejestr Cen Nieruchomości (dane transakcyjne GUGiK)

Od 13.02.2026 GUGiK udostępnia bezpłatnie zintegrowany RCN (362 powiaty) jako WFS:
`https://mapy.geoportal.gov.pl/wss/service/rcn` (warstwy `ms:lokale`, `ms:budynki`, `ms:dzialki`).

## Sync → MongoDB `rcn_transactions`
`scripts/rcn_sync.py` — pobiera lokale mieszkalne i działki niezabudowane w promieniu 7–18 km od centrów
~196 miast (`scripts/city-coords.js`), filtruje śmieci (udziały, absurdalne zł/m²) i upsertuje po `gid`.
Workflow: `.github/workflows/rcn-refresh.yml` (1. i 15. dnia miesiąca + ręcznie, z opcją jednego miasta).

Ważne (sprawdzone empirycznie): BBOX w FES w EPSG:2180 z kolejnością osi **(northing, easting)**;
filtr `DOK_DATA > 'YYYY-MM-DD'`; paging `COUNT=2000&STARTINDEX=n`.

## Użycie w aplikacji
- `GET /api/rcn/stats?city=&type=mieszkanie&area=&lat=&lon=&market=` → mediana, kwartyle, n, trend, 12 ostatnich transakcji.
- `oferta.html` — słupek „RCN” + lista transakcji; `build_valuation_pdf` — „Realna cena transakcyjna”.
- Domy: RCN nie ma powierzchni użytkowej budynków → wycena domów pozostaje na danych ofertowych.
