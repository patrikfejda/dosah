"""Tests for server.geometry — circle union → GeoJSON isochrone."""

import math

import pytest

from server.geometry import (
    circles_to_geojson_feature,
    crowfly_m,
    geodesic_area_km2,
)

BA_LAT, BA_LON = 48.1486, 17.1077


def test_crowfly_known_distance() -> None:
    # Hlavná stanica → Šafárikovo nám. is roughly 2.1 km
    d = crowfly_m(48.1585, 17.1067, 48.1404, 17.1155)
    assert 1900 < d < 2300


def test_crowfly_zero() -> None:
    assert crowfly_m(BA_LAT, BA_LON, BA_LAT, BA_LON) == pytest.approx(0.0)


def test_single_circle_area() -> None:
    # one 1000 m circle → area ≈ π km²
    feature = circles_to_geojson_feature([(BA_LAT, BA_LON, 1000.0)])
    area = feature["properties"]["area_km2"]
    assert area == pytest.approx(math.pi, rel=0.05)
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_disjoint_circles_multipolygon() -> None:
    # two far-apart circles must yield two parts and ~2π km² total
    feature = circles_to_geojson_feature(
        [(BA_LAT, BA_LON, 1000.0), (48.20, 17.20, 1000.0)]
    )
    assert feature["geometry"]["type"] == "MultiPolygon"
    assert len(feature["geometry"]["coordinates"]) == 2
    assert feature["properties"]["area_km2"] == pytest.approx(2 * math.pi, rel=0.05)


def test_overlapping_circles_merge() -> None:
    # two heavily overlapping circles merge into one polygon, area < 2π
    feature = circles_to_geojson_feature(
        [(BA_LAT, BA_LON, 1000.0), (BA_LAT + 0.001, BA_LON, 1000.0)]
    )
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["area_km2"] < 2 * math.pi


def test_coordinates_are_wgs84_near_input() -> None:
    feature = circles_to_geojson_feature([(BA_LAT, BA_LON, 500.0)])
    geom = feature["geometry"]
    rings = geom["coordinates"] if geom["type"] == "Polygon" else geom["coordinates"][0]
    lon, lat = rings[0][0]
    assert abs(lat - BA_LAT) < 0.02 and abs(lon - BA_LON) < 0.02


def test_empty_input_rejected() -> None:
    with pytest.raises(ValueError):
        circles_to_geojson_feature([])


def test_geodesic_area_of_known_polygon() -> None:
    # ~1 km × 1 km box near Bratislava (1° lat ≈ 111.32 km; lon scaled by cos φ)
    dlat = 1.0 / 111.32
    dlon = 1.0 / (111.32 * math.cos(math.radians(BA_LAT)))
    ring = [
        (BA_LON, BA_LAT),
        (BA_LON + dlon, BA_LAT),
        (BA_LON + dlon, BA_LAT + dlat),
        (BA_LON, BA_LAT + dlat),
        (BA_LON, BA_LAT),
    ]
    geom = {"type": "Polygon", "coordinates": [ring]}
    assert geodesic_area_km2(geom) == pytest.approx(1.0, rel=0.02)
