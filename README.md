# Dosah — Kam sa dostanem?

*Zadaj minúty. Uvidíš, kam až siahaš.*

Webová appka pre Bratislavu a okolie: vyber bod na mape, nastav časový budget
(5–60 min) a mapa ukáže reálnu oblasť, kam sa za ten čas dostaneš **pešo /
bicyklom / MHD / autom**.

## Spustenie

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server.app:app --port 8000
```

Otvor <http://localhost:8000>.

Pri štarte server načíta GTFS cestovné poriadky DPB z `data/gtfs.zip`
(pár sekúnd). Bez internetu funguje len režim MHD; pešo/bicykel/auto potrebujú
verejné Valhalla API.

## Ako to počíta

| Režim | Zdroj |
|---|---|
| Pešo / Bicykel / Auto | Vlastný router: OSM extrakty SK + AT + CZ + HU + juh PL → graf (`scripts/build_graph.py` → `data/streets.npz`, ~32 M uzlov) → scipy Dijkstra. Auto teda ide aj za hranice (Viedeň ~50 min). Ak graf chýba, fallback na [FOSSGIS Valhalla](https://valhalla1.openstreetmap.de) API (max 60 min). |
| MHD + vlak | Lokálny RAPTOR algoritmus nad GTFS feedmi DPB (mestská doprava) a ZSSK/ŽSR (vlaky) + únia peších dochádzkových kružníc okolo dosiahnutých zastávok |

MHD model: chôdza k zastávkam (4,8 km/h, faktor okľuky 1,3), max 3 prestupy,
60 s prestupný buffer, odchod v zvolený čas (default „teraz"). Načítavajú sa
dva prevádzkové dni, takže dlhé budgety cez polnoc vidia aj ranné spoje.
Časový budget: 5 min až 10 hodín.

## Aktualizácia dát

DPB feed (aktualizuje niekoľkokrát mesačne) a vlakový feed ZSSK/ŽSR:

```bash
curl -L -o data/gtfs.zip 'https://www.arcgis.com/sharing/rest/content/items/aba12fd2cbac4843bc7406151bc66106/data'
curl -L -o data/gtfs_rail.zip 'https://data.slovensko.sk/download?id=57827444-bb2e-4839-b1af-02241179a526'
```

Cestný graf (OSM extrakty; skript zmerguje všetky `data/*-latest.osm.pbf`):

```bash
for f in europe/slovakia europe/austria europe/czech-republic europe/hungary \
         europe/poland/malopolskie europe/poland/slaskie; do
  curl -L -o "data/$(basename $f)-latest.osm.pbf" "https://download.geofabrik.de/$f-latest.osm.pbf"
done
.venv/bin/python scripts/build_graph.py   # ~6 min, výsledok data/streets.npz
```

## Testy

```bash
.venv/bin/python -m pytest -q
```

## Štruktúra

- `server/` — FastAPI backend (GTFS loader, RAPTOR, Valhalla proxy, geometria)
- `web/` — statický frontend (Leaflet, vanilla JS)
- `tests/` — pytest (syntetické GTFS fixtures, žiadna závislosť na reálnom feede)
- `ARCHITECTURE.md`, `PRODUCT.md` — technická a produktová špecifikácia

## Dáta a atribúcia

- Mapové dlaždice a routing: © prispievatelia [OpenStreetMap](https://www.openstreetmap.org/copyright)
- Izochróny pešo/bicykel/auto: Valhalla (FOSSGIS, fair-use)
- MHD: GTFS © Dopravný podnik Bratislava, a. s. ([data.bratislava.sk](https://data.bratislava.sk/pages/gtfs_navod))
