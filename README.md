# Dosah — Kam sa dostanem?

*Zadaj minúty. Uvidíš, kam až siahaš.*

Webová appka pre Bratislavu a okolie: vyber bod na mape, nastav časový budget
(**5 min – 10 h**) a mapa ukáže reálnu oblasť, kam sa za ten čas dostaneš
**pešo / bicyklom / MHD + vlakom / autom**.

Všetko sa počíta lokálne — žiadne API kľúče, žiadne platené služby:

| Režim | Engine |
|---|---|
| Pešo / Bicykel / Auto | Vlastný router: OSM extrakty → kompaktný graf → scipy Dijkstra. Auto pokrýva strednú Európu (10 h ≈ Berlín/Miláno), pešo a bicykel Slovensko + Viedeň/Brno/severné Maďarsko. |
| MHD + vlak | Vlastný RAPTOR algoritmus nad GTFS cestovnými poriadkami DPB (mestská doprava BA) a ZSSK/ŽSR (vlaky celé SK vrátane medzinárodných spojov). Načítavajú sa dva prevádzkové dni, takže nočný 10-hodinový dotaz vidí aj ranné spoje. |

Bez lokálneho grafu appka automaticky spadne na verejné
[FOSSGIS Valhalla](https://valhalla1.openstreetmap.de) API (limit 60 min) —
takže funguje aj pred stiahnutím OSM dát.

## Ako na to

### 0. Požiadavky

- Python ≥ 3.12, ~2 GB disku pre malý variant (SK), ~20 GB pre plný
- RAM podľa zvoleného grafu (viď tabuľka nižšie)

### 1. Inštalácia

```bash
git clone https://github.com/patrikfejda/dosah.git && cd dosah
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Cestovné poriadky (MHD + vlaky)

```bash
curl -L -o data/gtfs.zip 'https://www.arcgis.com/sharing/rest/content/items/aba12fd2cbac4843bc7406151bc66106/data'
curl -L -o data/gtfs_rail.zip 'https://data.slovensko.sk/download?id=57827444-bb2e-4839-b1af-02241179a526'
```

DPB feed sa aktualizuje niekoľkokrát mesačne, vlakový platí do decembra —
po expirácii stačí stiahnuť znova (režim MHD inak vráti 503).

### 3. Cestný graf (voliteľné, ale odporúčané)

Vyber si variant podľa hardvéru — skript zmerguje všetky
`data/*-latest.osm.pbf`, ktoré nájde:

| Variant | Extrakty | Build | Súbor | RAM servera | Auto dosah |
|---|---|---|---|---|---|
| **SK** | slovakia | ~40 s | ~150 MB | ~2 GB | orezané na hraniciach SR |
| **SK + susedia** | + austria, czech-republic, hungary, poland/malopolskie, poland/slaskie | ~6 min | ~590 MB | ~5 GB | Viedeň, Brno, Budapešť |
| **Stredná Európa** | + germany, poland, switzerland, italy/nord-est, italy/nord-ovest, slovenia, croatia, romania, serbia, ukraine | ~25 min | ~2,1 GB | ~10 GB | Berlín, Miláno, Bukurešť |

```bash
# napr. variant SK + susedia:
for f in europe/slovakia europe/austria europe/czech-republic europe/hungary \
         europe/poland/malopolskie europe/poland/slaskie; do
  curl -L -o "data/$(basename $f)-latest.osm.pbf" "https://download.geofabrik.de/$f-latest.osm.pbf"
done
.venv/bin/python scripts/build_graph.py   # → data/streets.npz
```

PBF súbory môžeš po builde zmazať, graf ich už nepotrebuje.

### 4. Spustenie

```bash
.venv/bin/uvicorn server.app:app --port 8000
```

Otvor <http://localhost:8000>. Štart servera trvá podľa veľkosti grafu
(SK sekundy, stredná Európa jednotky minút — graf sa celý načítava do RAM).

## Ako to počíta

- **Ulice**: hrany grafu nesú triedu cesty, maxspeed a smerové/módové flagy
  (rýchlostný model podľa OSRM profilov). Izochróna = Dijkstra obmedzená
  budgetom, dosiahnuté uzly sa downsamplujú na adaptívnu mriežku a zjednotia
  do polygónu. Snapping štartu ide len na najväčší silne súvislý komponent
  a auto preferuje verejné cesty pred servisnými.
- **MHD**: chôdza k zastávkam (4,8 km/h, faktor okľuky 1,3), RAPTOR s max
  3 prestupmi a 60 s bufferom, z každej dosiahnutej zastávky pešací kruh zo
  zvyšného času; únia kruhov = izochróna.
- Známe limity: bez odbočovacích obmedzení, bez dopravy/zápch (free-flow),
  izochróny sa orežú na hranici stiahnutých dát.

## Testy

```bash
.venv/bin/python -m pytest -q   # 70 testov, syntetické fixtures, bez internetu
```

## Štruktúra

- `server/` — FastAPI, GTFS loader, RAPTOR, cestný router, geometria
- `scripts/build_graph.py` — OSM PBF → `data/streets.npz`
- `web/` — statický frontend (Leaflet, vanilla JS, bez build stepu)
- `ARCHITECTURE.md`, `PRODUCT.md` — technická a produktová špecifikácia

## Dáta a atribúcia

- Mapové dlaždice a cestná sieť: © prispievatelia
  [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL)
- MHD: GTFS © Dopravný podnik Bratislava, a. s.
  ([data.bratislava.sk](https://data.bratislava.sk/pages/gtfs_navod))
- Vlaky: GTFS ŽSR/ZSSK ([data.slovensko.sk](https://data.slovensko.sk))
- Fallback routing: Valhalla (FOSSGIS, fair-use)
