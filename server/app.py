"""Dosah FastAPI application: isochrone API + static frontend."""

from __future__ import annotations

import datetime
import logging
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from server import raptor
from server.geometry import circles_to_geojson_feature, geodesic_area_km2
from server.gtfs import FeedSpec, load_combined_network
from server.model import TransitNetwork
from server.streets import OriginTooFarError, StreetRouter, load_router
from server.streets_model import FOOT_BIKE_BBOX, GLOBAL_BBOX, in_bbox
from server.valhalla import ValhallaClient, ValhallaError

logger = logging.getLogger("dosah")

TZ = ZoneInfo("Europe/Bratislava")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GTFS_PATH = PROJECT_ROOT / "data" / "gtfs.zip"
RAIL_GTFS_PATH = PROJECT_ROOT / "data" / "gtfs_rail.zip"
# GTFS rail: classic type 2 plus extended railway types 100–117
RAIL_ROUTE_TYPES = frozenset({2, *range(100, 118)})
# Two consecutive service days so budgets crossing midnight keep working.
SERVICE_DAYS = 2


def _feed_specs() -> list[FeedSpec]:
    feeds = [FeedSpec(str(GTFS_PATH), "dpb:")]
    if RAIL_GTFS_PATH.exists():
        feeds.append(FeedSpec(str(RAIL_GTFS_PATH), "zsr:", RAIL_ROUTE_TYPES))
    return feeds


def _build_network(service_date: datetime.date) -> TransitNetwork:
    return load_combined_network(_feed_specs(), service_date, days=SERVICE_DAYS)
WEB_DIR = PROJECT_ROOT / "web"

STREETS_PATH = PROJECT_ROOT / "data" / "streets.npz"

WALK_SPEED = 1.33  # m/s
DETOUR = 1.3
MIN_MINUTES = 5
# Local street engine + local RAPTOR both support up to 10 hours. When the
# local street graph is missing, walk/bike/car fall back to Valhalla (60 min).
MAX_MINUTES = {"walk": 600, "bike": 600, "car": 600, "transit": 600}
VALHALLA_FALLBACK_MAX_MINUTES = 60
# Origin may be anywhere the street graph covers (GLOBAL_BBOX in
# server/streets_model.py — the single home of the coverage constants).
LAT_RANGE = (GLOBAL_BBOX[0], GLOBAL_BBOX[2])
LON_RANGE = (GLOBAL_BBOX[1], GLOBAL_BBOX[3])
MODE_TO_COSTING = {"walk": "pedestrian", "bike": "bicycle", "car": "auto"}
MAX_CACHED_DATES = 4  # explicit bound: FIFO cache of per-date GTFS networks
STREET_CACHE_MAX = 128  # explicit bound: FIFO cache of street isochrones

MSG_BAD_LAT_LON = (
    "Štart musí byť v pokrytom regióne (stredná Európa: "
    f"lat {LAT_RANGE[0]}–{LAT_RANGE[1]}, lon {LON_RANGE[0]}–{LON_RANGE[1]})."
)
MSG_FOOT_BIKE_OUT_OF_SCOPE = (
    "Pešo a bicyklom počítame dosah len na Slovensku a v blízkom okolí "
    "(Viedeň, Brno, severné Maďarsko)."
)
MSG_BAD_MINUTES = "Parameter minutes musí byť celé číslo od 5 do 600."
MSG_BAD_MODE = "Parameter mode musí byť walk, bike, car alebo transit."
MSG_BAD_DEPART = "Parameter depart musí byť čas v tvare HH:MM."
MSG_BAD_DATE = "Parameter date musí byť dátum v tvare YYYY-MM-DD."
MSG_GTFS_UNAVAILABLE = "Cestovné poriadky MHD nie sú k dispozícii, skúste to neskôr."
MSG_UPSTREAM_FAILED = "Výpočet dosahu zlyhal na externej službe, skúste to znova."
MSG_STREETS_FAILED = "Výpočet dosahu v lokálnej cestnej sieti zlyhal, skúste to znova."
MSG_ORIGIN_OFF_NETWORK = (
    "Štart je príliš ďaleko od cestnej siete (max 2 km). Vyber bod bližšie k ceste."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.networks = {}  # dict[datetime.date, TransitNetwork], FIFO ≤ 4
    app.state.valhalla = ValhallaClient()
    app.state.streets = None
    app.state.street_cache = {}  # FIFO ≤ STREET_CACHE_MAX
    streets_started = time.perf_counter()
    try:
        app.state.streets = await run_in_threadpool(load_router, str(STREETS_PATH))
    except Exception:
        # Startup must survive a missing/broken graph: street modes fall back
        # to Valhalla (60-min cap) until scripts/build_graph.py produces it.
        logger.exception(
            "street graph load failed, walk/bike/car fall back to Valhalla"
        )
    else:
        logger.info(
            "street graph ready in %.1f s: %d nodes, %d edge rows",
            time.perf_counter() - streets_started,
            app.state.streets.num_nodes,
            app.state.streets.num_edges,
        )
    today = datetime.datetime.now(TZ).date()
    started = time.perf_counter()
    try:
        network = _build_network(today)
    except Exception:
        # Startup must survive a broken feed: walk/bike/car keep working,
        # transit answers 503 until a load succeeds.
        logger.exception("GTFS startup load failed, transit disabled")
    else:
        app.state.networks[today] = network
        logger.info(
            "GTFS loaded for %s in %.1f s: %d stops, %d patterns, %d trips",
            today.isoformat(),
            time.perf_counter() - started,
            len(network.stop_coords),
            len(network.patterns),
            sum(len(p.trip_departures) for p in network.patterns),
        )
    yield
    await app.state.valhalla.aclose()


app = FastAPI(title="Dosah", lifespan=lifespan)


def _parse_float(value: str | None, low: float, high: float, message: str) -> float:
    if value is None:
        raise HTTPException(status_code=400, detail=message)
    try:
        number = float(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=message) from exc
    if not low <= number <= high:
        raise HTTPException(status_code=400, detail=message)
    return number


def _parse_minutes(value: str | None, mode: str) -> int:
    if value is None:
        raise HTTPException(status_code=400, detail=MSG_BAD_MINUTES)
    try:
        minutes = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=MSG_BAD_MINUTES) from exc
    if not MIN_MINUTES <= minutes <= MAX_MINUTES[mode]:
        raise HTTPException(status_code=400, detail=MSG_BAD_MINUTES)
    return minutes


def _parse_depart(value: str | None) -> int:
    """``HH:MM`` → seconds since midnight; default = now in Europe/Bratislava."""
    if value is None:
        now = datetime.datetime.now(TZ)
        return now.hour * 3600 + now.minute * 60
    parts = value.split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=MSG_BAD_DEPART)
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=MSG_BAD_DEPART) from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise HTTPException(status_code=400, detail=MSG_BAD_DEPART)
    return hours * 3600 + minutes * 60


def _parse_date(value: str | None) -> datetime.date:
    if value is None:
        return datetime.datetime.now(TZ).date()
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=MSG_BAD_DATE) from exc


async def _get_network(app: FastAPI, service_date: datetime.date) -> TransitNetwork:
    """Cached per-date network; rebuilds (in a thread) for uncached dates."""
    networks: dict[datetime.date, TransitNetwork] = app.state.networks
    cached = networks.get(service_date)
    if cached is not None:
        return cached
    try:
        network = await run_in_threadpool(_build_network, service_date)
    except Exception as exc:
        logger.exception("GTFS load for %s failed", service_date.isoformat())
        raise HTTPException(status_code=503, detail=MSG_GTFS_UNAVAILABLE) from exc
    if len(networks) >= MAX_CACHED_DATES:
        networks.pop(next(iter(networks)))
    networks[service_date] = network
    return network


async def _local_street_isochrone(
    request: Request, lat: float, lon: float, minutes: int, mode: str, started: float
) -> dict[str, Any]:
    router: StreetRouter = request.app.state.streets
    cache: dict[tuple[str, float, float, int], dict[str, Any]] = (
        request.app.state.street_cache
    )
    key = (mode, round(lat, 4), round(lon, 4), minutes)
    cached = cache.get(key)
    if cached is None:
        try:
            cached = await run_in_threadpool(
                router.isochrone, mode, lat, lon, minutes * 60
            )
        except OriginTooFarError as exc:
            # Deterministic user error, not an upstream failure: the point is
            # too far from any usable road — retrying cannot help.
            raise HTTPException(status_code=400, detail=MSG_ORIGIN_OFF_NETWORK) from exc
        except ValueError as exc:
            logger.warning("local %s isochrone failed: %s", mode, exc)
            raise HTTPException(status_code=502, detail=MSG_STREETS_FAILED) from exc
        cached["properties"]["compute_ms"] = int(
            (time.perf_counter() - started) * 1000
        )
        if len(cache) >= STREET_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = cached
    # Cache hits keep the original compute_ms — "0 ms" would look broken.
    props = dict(cached["properties"])
    props["engine"] = "local"
    feature = {"type": "Feature", "geometry": cached["geometry"], "properties": props}
    return {"type": "FeatureCollection", "features": [feature]}


async def _valhalla_street_isochrone(
    request: Request, lat: float, lon: float, minutes: int, mode: str, started: float
) -> dict[str, Any]:
    if minutes > VALHALLA_FALLBACK_MAX_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Režim {mode} podporuje max 60 min, kým nie je pripravený "
                "lokálny graf (spusti scripts/build_graph.py)."
            ),
        )
    try:
        raw = await request.app.state.valhalla.isochrone_feature(
            lat, lon, MODE_TO_COSTING[mode], minutes
        )
        area_km2 = geodesic_area_km2(raw["geometry"])
    except (ValhallaError, ValueError) as exc:
        logger.warning("valhalla %s isochrone failed: %s", mode, exc)
        raise HTTPException(status_code=502, detail=MSG_UPSTREAM_FAILED) from exc
    compute_ms = int((time.perf_counter() - started) * 1000)
    feature = {
        "type": "Feature",
        "geometry": raw["geometry"],
        "properties": {
            "mode": mode,
            "minutes": minutes,
            "area_km2": round(area_km2, 2),
            "compute_ms": compute_ms,
            "engine": "valhalla",
        },
    }
    return {"type": "FeatureCollection", "features": [feature]}


async def _street_isochrone(
    request: Request, lat: float, lon: float, minutes: int, mode: str, started: float
) -> dict[str, Any]:
    if request.app.state.streets is not None:
        return await _local_street_isochrone(request, lat, lon, minutes, mode, started)
    return await _valhalla_street_isochrone(request, lat, lon, minutes, mode, started)


async def _transit_isochrone(
    request: Request,
    lat: float,
    lon: float,
    minutes: int,
    depart: str | None,
    date: str | None,
    started: float,
) -> dict[str, Any]:
    service_date = _parse_date(date)
    depart_s = _parse_depart(depart)
    network = await _get_network(request.app, service_date)

    budget_s = minutes * 60
    arrivals = raptor.reachable_stops(network, lat, lon, depart_s, budget_s)
    limit = depart_s + budget_s

    circles: list[tuple[float, float, float]] = [
        (lat, lon, budget_s * WALK_SPEED / DETOUR)
    ]
    for stop_id, arrival in arrivals.items():
        remaining_s = limit - arrival
        if remaining_s <= 0:
            continue
        stop_lat, stop_lon = network.stop_coords[stop_id]
        circles.append((stop_lat, stop_lon, remaining_s * WALK_SPEED / DETOUR))

    compute_ms = int((time.perf_counter() - started) * 1000)
    feature = circles_to_geojson_feature(
        circles,
        {
            "mode": "transit",
            "minutes": minutes,
            "compute_ms": compute_ms,
            "engine": "raptor",
        },
    )
    return {"type": "FeatureCollection", "features": [feature]}


@app.get("/api/isochrone")
async def isochrone(
    request: Request,
    lat: str | None = None,
    lon: str | None = None,
    minutes: str | None = None,
    mode: str | None = None,
    depart: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    lat_f = _parse_float(lat, *LAT_RANGE, MSG_BAD_LAT_LON)
    lon_f = _parse_float(lon, *LON_RANGE, MSG_BAD_LAT_LON)
    if mode not in ("walk", "bike", "car", "transit"):
        raise HTTPException(status_code=400, detail=MSG_BAD_MODE)
    if mode in ("walk", "bike") and not in_bbox(lat_f, lon_f, FOOT_BIKE_BBOX):
        raise HTTPException(status_code=400, detail=MSG_FOOT_BIKE_OUT_OF_SCOPE)
    minutes_i = _parse_minutes(minutes, mode)
    if mode == "transit":
        return await _transit_isochrone(
            request, lat_f, lon_f, minutes_i, depart, date, started
        )
    return await _street_isochrone(request, lat_f, lon_f, minutes_i, mode, started)


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    networks: dict[datetime.date, TransitNetwork] = request.app.state.networks
    router: StreetRouter | None = request.app.state.streets
    streets_info: dict[str, Any] = {"loaded": router is not None}
    if router is not None:
        streets_info["nodes"] = router.num_nodes
        streets_info["edges"] = router.num_edges
    if not networks:
        return {"status": "ok", "gtfs": {"loaded": False}, "streets": streets_info}
    latest_date = next(reversed(networks))
    network = networks[latest_date]
    return {
        "status": "ok",
        "gtfs": {
            "loaded": True,
            "stops": len(network.stop_coords),
            "patterns": len(network.patterns),
            "trips": sum(len(p.trip_departures) for p in network.patterns),
            "service_date": network.service_date,
        },
        "streets": streets_info,
    }


# Static frontend mounted last so /api/* routes win; guarded — the web/
# directory is built by another teammate and may not exist yet.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
