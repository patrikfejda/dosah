"""Geometry helpers: crow-fly distance, circle unions, GeoJSON output.

All public functions take/return WGS84 coordinates. Internally circles are
built in a local azimuthal-equidistant-like flat approximation around
Bratislava, which is accurate to well under 1 % at this scale (< 50 km).
"""

from __future__ import annotations

import math
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

EARTH_RADIUS_M = 6_371_000.0
# metres per degree of latitude (near-constant)
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0

SIMPLIFY_TOLERANCE_M = 25.0
CIRCLE_SEGMENTS = 24


def crowfly_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class _LocalProjection:
    """Flat equirectangular projection centred on a reference latitude."""

    def __init__(self, ref_lat: float, ref_lon: float) -> None:
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(ref_lat))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            (lon - self.ref_lon) * self.m_per_deg_lon,
            (lat - self.ref_lat) * M_PER_DEG_LAT,
        )

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.ref_lon + x / self.m_per_deg_lon,
            self.ref_lat + y / M_PER_DEG_LAT,
        )


def circles_to_geojson_feature(
    circles: list[tuple[float, float, float]],
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Union of (lat, lon, radius_m) circles → GeoJSON Feature in WGS84.

    Properties always include ``area_km2`` (union area). Raises ValueError on
    empty input or non-positive radii only (zero-radius circles are dropped).
    """
    usable = [(lat, lon, r) for lat, lon, r in circles if r > 0]
    if not usable:
        raise ValueError("no circles with positive radius")

    ref_lat = sum(c[0] for c in usable) / len(usable)
    ref_lon = sum(c[1] for c in usable) / len(usable)
    proj = _LocalProjection(ref_lat, ref_lon)

    discs = [
        Point(proj.to_xy(lat, lon)).buffer(r, quad_segs=CIRCLE_SEGMENTS // 4)
        for lat, lon, r in usable
    ]
    union = unary_union(discs).simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)
    area_km2 = union.area / 1e6

    geom = _project_geometry_to_wgs84(union, proj)
    props: dict[str, Any] = dict(properties or {})
    props["area_km2"] = round(area_km2, 2)
    return {"type": "Feature", "geometry": geom, "properties": props}


def _ring_to_lonlat(coords: Any, proj: _LocalProjection) -> list[list[float]]:
    return [[round(v, 6) for v in proj.to_lonlat(x, y)] for x, y in coords]


def _project_geometry_to_wgs84(
    geom: BaseGeometry, proj: _LocalProjection
) -> dict[str, Any]:
    if isinstance(geom, Polygon):
        rings = [_ring_to_lonlat(geom.exterior.coords, proj)] + [
            _ring_to_lonlat(hole.coords, proj) for hole in geom.interiors
        ]
        return {"type": "Polygon", "coordinates": rings}
    if isinstance(geom, MultiPolygon):
        polys = [
            [_ring_to_lonlat(p.exterior.coords, proj)]
            + [_ring_to_lonlat(hole.coords, proj) for hole in p.interiors]
            for p in geom.geoms
        ]
        return {"type": "MultiPolygon", "coordinates": polys}
    raise ValueError(f"unexpected geometry type from union: {geom.geom_type}")


def geodesic_area_km2(geojson_geometry: dict[str, Any]) -> float:
    """Approximate area of a WGS84 GeoJSON (Multi)Polygon in km²."""
    geom = shape(geojson_geometry)
    if isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    elif isinstance(geom, Polygon):
        polys = [geom]
    else:
        raise ValueError(f"unsupported geometry type: {geom.geom_type}")
    total = 0.0
    for poly in polys:
        centroid_lat = poly.centroid.y
        proj = _LocalProjection(centroid_lat, poly.centroid.x)
        ext = Polygon(
            [proj.to_xy(lat, lon) for lon, lat in poly.exterior.coords],
            holes=[
                [proj.to_xy(lat, lon) for lon, lat in hole.coords]
                for hole in poly.interiors
            ],
        )
        total += ext.area
    return total / 1e6
