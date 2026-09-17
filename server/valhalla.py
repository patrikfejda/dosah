"""Async client for the public FOSSGIS Valhalla isochrone service.

Fair use: identifies itself with a proper User-Agent, 10 s timeout, exactly
one retry, results cached in memory (FIFO, capped at 256 entries).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VALHALLA_URL = "https://valhalla1.openstreetmap.de/isochrone"
USER_AGENT = "dosah-isochrone-app/1.0 (local dev)"
REQUEST_TIMEOUT_S = 10.0
MAX_ATTEMPTS = 2  # explicit bound: 1 initial request + 1 retry
CACHE_MAX_ENTRIES = 256  # explicit bound: FIFO eviction of the oldest entry

_CacheKey = tuple[float, float, str, int]


class ValhallaError(Exception):
    """Valhalla upstream failed or returned an unusable payload."""


class ValhallaClient:
    """Fetches single-contour isochrone features, with an in-memory cache."""

    def __init__(self, base_url: str = VALHALLA_URL) -> None:
        self._base_url = base_url
        # dict preserves insertion order → FIFO eviction via first key.
        self._cache: dict[_CacheKey, dict[str, Any]] = {}
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def isochrone_feature(
        self, lat: float, lon: float, costing: str, minutes: int
    ) -> dict[str, Any]:
        """GeoJSON Feature of the ``minutes`` isochrone around (lat, lon)."""
        key: _CacheKey = (round(lat, 4), round(lon, 4), costing, minutes)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        feature = await self._fetch(lat, lon, costing, minutes)
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = feature
        return feature

    async def _fetch(
        self, lat: float, lon: float, costing: str, minutes: int
    ) -> dict[str, Any]:
        body = {
            "locations": [{"lat": lat, "lon": lon}],
            "costing": costing,
            "contours": [{"time": minutes}],
            "polygons": True,
            "denoise": 0.3,
            "generalize": 50,
        }
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(self._base_url, json=body)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("valhalla attempt %d network error: %s", attempt, exc)
                continue
            if response.status_code >= 500:
                last_error = ValhallaError(f"upstream status {response.status_code}")
                logger.warning("valhalla attempt %d: HTTP %d", attempt, response.status_code)
                continue
            if response.status_code != 200:
                raise ValhallaError(
                    f"upstream status {response.status_code}: {response.text[:200]}"
                )
            return _extract_feature(response)
        raise ValhallaError(
            f"request failed after {MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error


def _extract_feature(response: httpx.Response) -> dict[str, Any]:
    """First feature of the returned GeoJSON FeatureCollection."""
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ValhallaError(f"upstream returned invalid JSON: {exc}") from exc
    features = payload.get("features") if isinstance(payload, dict) else None
    if not features:
        raise ValhallaError("upstream response is not a GeoJSON FeatureCollection")
    feature = features[0]
    if not isinstance(feature, dict) or "geometry" not in feature:
        raise ValhallaError("upstream feature has no geometry")
    return feature
