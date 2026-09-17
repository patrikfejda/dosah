"""Tests for StreetRouter on a tiny synthetic in-memory graph."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from server.streets import OriginTooFarError, StreetRouter
from server.streets_model import CAR_BWD, CAR_FWD, CLASS_ID

LAT0 = 48.14
LON0 = 17.10
M_PER_DEG_LAT = math.pi * 6_371_000.0 / 180.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def _line_arrays() -> dict[str, np.ndarray]:
    """4 nodes in a west-east line, 1 km apart, car-only motorway edges."""
    return {
        "lats": np.full(4, LAT0, dtype=np.float64),
        "lons": LON0 + np.arange(4, dtype=np.float64) * (1000.0 / M_PER_DEG_LON),
        "u": np.array([0, 1, 2], dtype=np.int32),
        "v": np.array([1, 2, 3], dtype=np.int32),
        "length_m": np.full(3, 1000.0, dtype=np.float32),
        "highway_class": np.full(3, CLASS_ID["motorway"], dtype=np.uint8),
        "maxspeed_kmh": np.zeros(3, dtype=np.uint16),
        "flags": np.full(3, CAR_FWD | CAR_BWD, dtype=np.uint8),
    }


def _line_router() -> StreetRouter:
    arrays = _line_arrays()
    return StreetRouter(
        arrays["lats"],
        arrays["lons"],
        arrays["u"],
        arrays["v"],
        arrays["length_m"],
        arrays["highway_class"],
        arrays["maxspeed_kmh"],
        arrays["flags"],
    )


def test_car_budget_reaches_partial_line() -> None:
    router = _line_router()
    # motorway default 90 km/h → 1 km takes 40 s; 60 s budget reaches nodes 0, 1.
    indices, costs = router.reachable_costs("car", LAT0, LON0, 60)
    assert len(indices) == 2
    by_node = dict(zip(indices.tolist(), costs.tolist()))
    assert by_node[0] == pytest.approx(0.0, abs=1e-6)
    assert by_node[1] == pytest.approx(40.0, rel=0.01)


def test_car_large_budget_reaches_all_nodes() -> None:
    router = _line_router()
    indices, _ = router.reachable_costs("car", LAT0, LON0, 600)
    assert len(indices) == 4


def test_walk_mode_without_foot_edges_raises_domain_error() -> None:
    router = _line_router()
    with pytest.raises(OriginTooFarError):
        router.isochrone("walk", LAT0, LON0, 900)


def test_isochrone_returns_valid_geojson_feature() -> None:
    router = _line_router()
    feature = router.isochrone("car", LAT0, LON0, 60)
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    props = feature["properties"]
    assert props["mode"] == "car"
    assert props["minutes"] == 1
    assert props["area_km2"] > 0


def test_non_positive_budget_raises_value_error() -> None:
    router = _line_router()
    with pytest.raises(ValueError):
        router.isochrone("car", LAT0, LON0, 0)
    with pytest.raises(ValueError):
        router.isochrone("car", LAT0, LON0, -5)


def test_unknown_mode_raises_value_error() -> None:
    router = _line_router()
    with pytest.raises(ValueError):
        router.isochrone("transit", LAT0, LON0, 600)


def test_snap_beyond_2km_raises_domain_error() -> None:
    router = _line_router()
    with pytest.raises(OriginTooFarError):
        router.isochrone("car", LAT0 + 0.05, LON0, 600)  # ~5.5 km north


def test_grid_snap_picks_nearest_node() -> None:
    router = _line_router()
    # Query 300 m east of node 1 → node 1 (700 m to node 2).
    query_lon = LON0 + 1300.0 / M_PER_DEG_LON
    node, dist = router._snap("car", LAT0, query_lon)
    assert node == 1
    assert dist == pytest.approx(300.0, rel=0.01)


def test_grid_snap_finds_node_beyond_first_ring() -> None:
    router = _line_router()
    # 1500 m north of node 0: farther than one 600 m grid cell, still < 2 km,
    # so the ring expansion (3×3 → 5×5 → ...) must find it.
    node, dist = router._snap("car", LAT0 + 1500.0 / M_PER_DEG_LAT, LON0)
    assert node == 0
    assert dist == pytest.approx(1500.0, rel=0.01)


def test_car_snap_prefers_public_road_over_service() -> None:
    # Motorway line 0-1-2-3 plus a service-only node 4 hanging 300 m north of
    # node 3. Query right next to node 4: the public-road node 3 is farther
    # but must win (service/farm webs are useless as a start).
    arrays = _line_arrays()
    lats = np.append(arrays["lats"], LAT0 + 300.0 / M_PER_DEG_LAT)
    lons = np.append(arrays["lons"], arrays["lons"][3])
    u = np.append(arrays["u"], np.int32(3))
    v = np.append(arrays["v"], np.int32(4))
    length_m = np.append(arrays["length_m"], np.float32(300.0))
    highway_class = np.append(arrays["highway_class"], np.uint8(CLASS_ID["service"]))
    maxspeed_kmh = np.append(arrays["maxspeed_kmh"], np.uint16(0))
    flags = np.append(arrays["flags"], np.uint8(CAR_FWD | CAR_BWD))
    router = StreetRouter(
        lats, lons, u, v, length_m, highway_class, maxspeed_kmh, flags
    )
    query_lat = LAT0 + 290.0 / M_PER_DEG_LAT  # 10 m from node 4, 290 m from node 3
    node, dist = router._snap("car", query_lat, float(lons[3]))
    assert node == 3
    assert dist == pytest.approx(290.0, rel=0.05)


def test_car_snap_falls_back_to_service_when_no_public_in_reach() -> None:
    # Service-only edges: no public car node exists → full car grid answers.
    arrays = _line_arrays()
    arrays["highway_class"] = np.full(3, CLASS_ID["service"], dtype=np.uint8)
    router = StreetRouter(
        arrays["lats"],
        arrays["lons"],
        arrays["u"],
        arrays["v"],
        arrays["length_m"],
        arrays["highway_class"],
        arrays["maxspeed_kmh"],
        arrays["flags"],
    )
    node, dist = router._snap("car", LAT0, LON0)
    assert node == 0
    assert dist == pytest.approx(0.0, abs=1.0)


def test_npz_round_trip_with_masks_uses_masks(tmp_path: Path) -> None:
    # snap_ok_car excludes node 0: a query at node 0 must snap to node 1 even
    # though node 0 is nearest — proof the precomputed masks are honoured
    # (the runtime fallback would have snapped to node 0).
    snap_ok_car = np.array([False, True, True, True])
    payload: dict[str, Any] = {
        **_line_arrays(),
        "snap_ok_walk": np.zeros(4, dtype=bool),
        "snap_ok_bike": np.zeros(4, dtype=bool),
        "snap_ok_car": snap_ok_car,
        "snap_car_public": snap_ok_car,
    }
    path = tmp_path / "streets_masked.npz"
    np.savez(path, **payload)
    router = StreetRouter.from_npz(str(path))
    node, dist = router._snap("car", LAT0, LON0)
    assert node == 1
    assert dist == pytest.approx(1000.0, rel=0.01)
    # Empty walk mask → walk is unusable even though FOOT-free graph would
    # raise anyway; assert the error type stays the same.
    with pytest.raises(OriginTooFarError):
        router._snap("walk", LAT0, LON0)


def test_npz_round_trip_without_masks_uses_fallback(tmp_path: Path) -> None:
    payload: dict[str, Any] = dict(_line_arrays())
    path = tmp_path / "streets_plain.npz"
    np.savez(path, **payload)
    router = StreetRouter.from_npz(str(path))
    node, dist = router._snap("car", LAT0, LON0)
    assert node == 0
    assert dist == pytest.approx(0.0, abs=1.0)
    indices, _ = router.reachable_costs("car", LAT0, LON0, 600)
    assert len(indices) == 4
