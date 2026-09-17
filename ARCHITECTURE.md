# Dokial (How much can I reach?) — Architecture

One-screen web app: pick a point in/around Bratislava, pick a time budget, see the
real reachable area as a polygon on the map, for modes: walk / bike / public
transport (MHD) / car.

## Stack

- **Backend**: Python 3.14, FastAPI + uvicorn. Serves the static frontend and a
  single JSON API. Dependencies kept minimal: `fastapi`, `uvicorn`, `httpx`,
  `shapely`. GTFS parsed with stdlib `csv`/`zipfile`.
- **Frontend**: static HTML/CSS/JS, Leaflet from CDN, OSM raster tiles.
  No build step. Slovak UI.
- **No database.** GTFS loaded into memory at startup from `data/gtfs.zip`.

## Isochrone computation

### walk / bike / car
Primary: our own local engine (`server/streets.py`) — OSM Slovakia extract
preprocessed by `scripts/build_graph.py` into `data/streets.npz` (compact
node/edge arrays with per-edge highway class, maxspeed and mode/direction
flags), loaded at startup into per-mode scipy CSR matrices; isochrone =
`scipy.sparse.csgraph.dijkstra` bounded by the budget, reached nodes
downsampled on an adaptive grid and unioned into a polygon. Supports
5–600 min. Speed/access model follows OSRM profile defaults.

Fallback: when `data/streets.npz` is missing, the public FOSSGIS Valhalla
server (`https://valhalla1.openstreetmap.de/isochrone`) is used instead —
capped at 60 min (their hard service limit). On failure → HTTP 502 with a
Slovak error message; frontend shows an error banner (no fake polygon).

Graph coverage: SK+AT+CZ+HU+DE+PL+CH+IT-north+SI+HR+RO+RS+UA extracts,
clipped to GLOBAL_BBOX 44.5–54.9 / 5.9–24.5 (car keeps everything inside).
Foot/bike flags are kept only on edges with both endpoints inside
FOOT_BIKE_BBOX 47.4–49.7 / 15.9–22.6 (SK + Vienna/Brno/Budapest fringe) —
walking from Berlin is out of product scope; the API pre-checks this and
answers 400 in Slovak. Snapping targets the giant strongly-connected
component only (precomputed at build time into snap_ok_* masks in the npz;
old files fall back to computing them at load), uses a 600 m bucket-grid
index instead of a KD-tree (a KD-tree over ~90 M nodes would cost minutes
and gigabytes at startup), and car prefers public roads over service/farm
webs (snap_car_public mask). Known limits: no turn restrictions, no live
traffic, isochrones clip at the bbox edge.

### MHD (transit)
Computed locally from GTFS with a RAPTOR-style algorithm. Two feeds are
merged with stop-id prefixes (`server/gtfs.py::load_combined_network`):
DPB city transit (`data/gtfs.zip`, prefix `dpb:`) and ZSSK/ŽSR railways
(`data/gtfs_rail.zip`, prefix `zsr:`, filtered to rail route_types
2 & 100–117). Two consecutive service days are loaded — day N+1 trips are
shifted +24 h so long budgets crossing midnight see the next morning's
departures. Trains transfer to city stops via the normal 300 m foot-transfer
relaxation.

1. **Departure time**: user-selectable, default = now (server local time),
   mapped to the GTFS service day (handles times > 24:00 for after-midnight).
2. **Access**: origin → every stop within crow-fly walking reach.
   `walk_time = (crowfly_m * DETOUR) / WALK_SPEED`, DETOUR = 1.3,
   WALK_SPEED = 1.33 m/s (~4.8 km/h).
3. **RAPTOR rounds** (max 4 rounds = 3 transfers, explicit bound):
   per round relax trips along each route touched by improved stops; then relax
   footpath transfers between nearby stops (crow-fly ≤ 300 m).
   `MIN_TRANSFER_S = 60` boarding buffer.
4. **Egress**: every reached stop with remaining budget `t_rem` contributes a
   circle of radius `t_rem * WALK_SPEED / DETOUR`; plus the origin's own
   walking circle. Union via shapely (in a local metric projection),
   simplify (~30 m tolerance), return GeoJSON MultiPolygon in WGS84.

Data structures (precomputed at startup):
- `stops`: id → (lat, lon); spatial grid index (~500 m cells) for radius queries.
- `patterns`: unique stop-sequences; each pattern has sorted trip departure
  arrays for binary search of "earliest trip catchable at stop i after time t".
- Only services active on the requested date (calendar + calendar_dates).

## API

```
GET /api/isochrone?lat=&lon=&minutes=&mode=walk|bike|car|transit[&depart=HH:MM][&date=YYYY-MM-DD]
→ 200 {"type":"FeatureCollection","features":[{geometry, properties:{mode,minutes,area_km2,compute_ms}}]}
→ 400 invalid params | 502 upstream failure | 503 GTFS not loaded (transit)
GET /api/health → {"status":"ok","gtfs":{"stops":N,"trips":N,"service_date":...}}
GET / → static frontend
```

Validation: minutes ∈ [5, 60]; lat/lon within bbox 47.9–48.4 / 16.8–17.5
(Bratislava + okolie), else 400.

## Layout

```
reach-shower/
  ARCHITECTURE.md  PRODUCT.md  README.md
  requirements.txt
  data/gtfs.zip
  server/
    app.py          # FastAPI app, routes, static mount
    valhalla.py     # proxy client + cache
    gtfs.py         # GTFS load/parse → in-memory model
    raptor.py       # transit isochrone algorithm
    geometry.py     # circle union, projection, area, GeoJSON helpers
  tests/
    test_raptor.py  # synthetic mini-GTFS, deterministic assertions
    test_geometry.py
  web/
    index.html  app.js  style.css
```

## Engineering rules (binding for all devs)

- Every retry/poll/recursion has an explicit numeric bound.
- No empty excepts; log or raise. Validate all inputs at the API edge.
- Functions do one job; flat control flow, early returns.
- Tests first for `raptor.py` and `geometry.py` (synthetic GTFS fixture —
  do NOT depend on the real feed in tests).
- Type hints throughout; code and identifiers in English, UI strings in Slovak.
