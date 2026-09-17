"""In-memory transit network model shared by the GTFS loader and RAPTOR.

Times are seconds since service-day midnight (GTFS convention, may exceed
24 h * 3600 for after-midnight trips).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from server.geometry import crowfly_m

GRID_CELL_M = 500.0
_M_PER_DEG_LAT = 111_320.0


@dataclass
class Pattern:
    """A unique ordered stop sequence with all its trips.

    Trips are sorted by departure time at the first stop and assumed FIFO
    (no overtaking within a pattern) — standard RAPTOR precondition.
    """

    stop_ids: list[str]
    trip_departures: list[list[int]]  # [trip][stop_idx] -> departure seconds
    trip_arrivals: list[list[int]]  # [trip][stop_idx] -> arrival seconds


@dataclass
class TransitNetwork:
    stop_coords: dict[str, tuple[float, float]]  # stop_id -> (lat, lon)
    patterns: list[Pattern]
    stop_patterns: dict[str, list[tuple[int, int]]]  # stop_id -> [(pattern, idx)]
    service_date: str  # YYYYMMDD the timetable was built for
    _grid: dict[tuple[int, int], list[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for stop_id, (lat, lon) in self.stop_coords.items():
            self._grid.setdefault(self._cell(lat, lon), []).append(stop_id)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat))
        return (
            int(lat * _M_PER_DEG_LAT / GRID_CELL_M),
            int(lon * m_per_deg_lon / GRID_CELL_M),
        )

    def stops_within(
        self, lat: float, lon: float, radius_m: float
    ) -> list[tuple[str, float]]:
        """All stops within crow-fly ``radius_m`` of a point, with distances."""
        if radius_m <= 0:
            return []
        span = int(radius_m // GRID_CELL_M) + 1
        c_lat, c_lon = self._cell(lat, lon)
        found: list[tuple[str, float]] = []
        for di in range(-span, span + 1):
            for dj in range(-span, span + 1):
                for stop_id in self._grid.get((c_lat + di, c_lon + dj), ()):
                    s_lat, s_lon = self.stop_coords[stop_id]
                    dist = crowfly_m(lat, lon, s_lat, s_lon)
                    if dist <= radius_m:
                        found.append((stop_id, dist))
        return found
