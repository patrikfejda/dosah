"""Local street-routing isochrone engine (walk / bike / car).

Loads the preprocessed graph from ``data/streets.npz`` (built by
``scripts/build_graph.py``), builds one directed travel-time CSR matrix per
mode plus a per-mode grid bucket index for origin snapping, and answers
isochrone queries via scipy's Dijkstra + a gridded circle union.

Snap-eligibility masks (giant strongly-connected component per mode, public
car nodes) are precomputed at build time and stored in the npz as
``snap_ok_walk`` / ``snap_ok_bike`` / ``snap_ok_car`` / ``snap_car_public``.
Old files without them fall back to computing the same masks at load time.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from server.geometry import M_PER_DEG_LAT, circles_to_geojson_feature
from server.streets_model import (
    BIKE_KMH,
    CAR_DEFAULT_KMH,
    CLASS_ID,
    FOOT_KMH,
    MODE_BITS,
    NUM_CLASSES,
)

logger = logging.getLogger(__name__)

_SERVICE_CLASS = CLASS_ID["service"]

MODES = ("walk", "bike", "car")

MAX_BUDGET_S = 600 * 60  # explicit bound: 10 hours
MAX_SNAP_M = 2_000.0
MAX_CIRCLES = 6_000  # explicit bound on circles handed to the union
# Walk gets a finer grid: its isochrones are small, and the min circle radius
# (RADIUS_MIN_CELL_FACTOR × cell) otherwise inflates the area noticeably.
MIN_CELL_M = {"walk": 90.0, "bike": 150.0, "car": 150.0}
RADIUS_MIN_CELL_FACTOR = 0.8
RADIUS_MAX_CELL_FACTOR = 2.5
CAR_SPEED_CAP_KMH = 130.0
CAR_MAXSPEED_FACTOR = 0.9

# Off-graph spillover speed at the isochrone fringe (m/s).
OFFROAD_MPS = {"walk": 1.0, "bike": 2.0, "car": 5.0}

# Origin's own off-road circle (matches the transit access model).
WALK_SPEED_MPS = 1.33
WALK_DETOUR = 1.3
ORIGIN_MIN_RADIUS_M = 150.0

# Snap grid: bucket cell size and ring bound. Ring r guarantees the exact
# nearest node for any hit within r * SNAP_CELL_M, so the last ring must
# cover more than MAX_SNAP_M (4 * 600 m = 2 400 m > 2 000 m).
SNAP_CELL_M = 600.0
SNAP_MAX_RING = math.ceil(MAX_SNAP_M / SNAP_CELL_M) + 1  # explicit bound: 4
_GRID_OFFSET = np.int64(1) << np.int64(20)  # keeps cell indices non-negative


class OriginTooFarError(Exception):
    """Origin cannot be snapped to any node usable by the requested mode."""


def _class_speed_table(kmh_by_class: dict[int, int]) -> np.ndarray:
    table = np.zeros(NUM_CLASSES, dtype=np.float64)
    for cls, kmh in kmh_by_class.items():
        table[cls] = float(kmh)
    return table


_CAR_DEFAULT_TABLE = _class_speed_table(CAR_DEFAULT_KMH)
_BIKE_TABLE = _class_speed_table(BIKE_KMH)


def _dedupe_min(
    rows: np.ndarray, cols: np.ndarray, secs: np.ndarray, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Keep the fastest edge per directed (u, v) pair (COO→CSR would sum)."""
    key = rows.astype(np.int64) * n + cols.astype(np.int64)
    order = np.lexsort((secs, key))
    key_sorted = key[order]
    first = np.empty(len(order), dtype=bool)
    first[:1] = True
    first[1:] = key_sorted[1:] != key_sorted[:-1]
    keep = order[first]
    return rows[keep], cols[keep], secs[keep]


class _SnapGrid:
    """Bucket grid over projected node coordinates for nearest-node queries.

    A KD-tree over ~90 M points takes minutes to build and gigabytes of RAM;
    this index is one argsort over int64 cell ids (600 m cells) plus
    ``searchsorted`` range lookups per query. It references the router's full
    xs/ys arrays instead of copying coordinates.
    """

    def __init__(self, node_idx: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> None:
        self._xs = xs
        self._ys = ys
        cells = self._cell_ids(xs[node_idx], ys[node_idx])
        order = np.argsort(cells, kind="stable")
        self._nodes = node_idx[order].astype(np.int64)
        cells_sorted = cells[order]
        del cells, order
        # Bucket boundaries: unique cells + start offset of each bucket.
        first = np.empty(len(cells_sorted), dtype=bool)
        first[:1] = True
        first[1:] = cells_sorted[1:] != cells_sorted[:-1]
        starts = np.flatnonzero(first)
        self._bucket_cells = cells_sorted[starts]
        self._bucket_starts = np.append(starts, np.int64(len(cells_sorted)))

    @staticmethod
    def _cell_ids(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        ix = np.floor(xs / SNAP_CELL_M).astype(np.int64) + _GRID_OFFSET
        iy = np.floor(ys / SNAP_CELL_M).astype(np.int64) + _GRID_OFFSET
        return (ix << np.int64(32)) | iy

    def _candidates_in_square(self, qix: int, qiy: int, ring: int) -> np.ndarray:
        """Node ids in the (2*ring+1)² cell square centred on (qix, qiy)."""
        chunks: list[np.ndarray] = []
        for di in range(-ring, ring + 1):
            base = np.int64(qix + di) << np.int64(32)
            lo_cell = base | np.int64(qiy - ring)
            hi_cell = base | np.int64(qiy + ring)
            b_lo = int(np.searchsorted(self._bucket_cells, lo_cell, side="left"))
            b_hi = int(np.searchsorted(self._bucket_cells, hi_cell, side="right"))
            if b_hi > b_lo:
                lo = int(self._bucket_starts[b_lo])
                hi = int(self._bucket_starts[b_hi])
                chunks.append(self._nodes[lo:hi])
        if not chunks:
            return self._nodes[:0]
        return np.concatenate(chunks)

    def query(self, x: float, y: float) -> tuple[int, float] | None:
        """(nearest node, distance m) within MAX_SNAP_M, else None.

        Expands 3×3 → 5×5 → ... up to SNAP_MAX_RING. A best hit at distance
        ≤ ring * SNAP_CELL_M is provably the global nearest (anything outside
        the square is farther), so results match the former KD-tree exactly.
        """
        qix = int(math.floor(x / SNAP_CELL_M)) + int(_GRID_OFFSET)
        qiy = int(math.floor(y / SNAP_CELL_M)) + int(_GRID_OFFSET)
        best: tuple[int, float] | None = None
        for ring in range(1, SNAP_MAX_RING + 1):
            cand = self._candidates_in_square(qix, qiy, ring)
            if len(cand) == 0:
                continue
            dx = self._xs[cand] - x
            dy = self._ys[cand] - y
            d2 = dx * dx + dy * dy
            k = int(d2.argmin())
            best = (int(cand[k]), float(math.sqrt(d2[k])))
            if best[1] <= ring * SNAP_CELL_M:
                break
        if best is None or best[1] > MAX_SNAP_M:
            return None
        return best


class StreetRouter:
    """Per-mode directed travel-time graphs + snap grids over usable nodes."""

    def __init__(
        self,
        lats: np.ndarray,
        lons: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        length_m: np.ndarray,
        highway_class: np.ndarray,
        maxspeed_kmh: np.ndarray,
        flags: np.ndarray,
        snap_masks: dict[str, np.ndarray] | None = None,
        snap_car_public: np.ndarray | None = None,
    ) -> None:
        self.num_nodes = int(len(lats))
        self.num_edges = int(len(u))
        self._lats = np.asarray(lats, dtype=np.float64)
        self._lons = np.asarray(lons, dtype=np.float64)

        ref_lat = float(self._lats.mean()) if self.num_nodes else 48.7
        self._m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
        self._xs = self._lons * self._m_per_deg_lon
        self._ys = self._lats * M_PER_DEG_LAT

        length = np.asarray(length_m, dtype=np.float64)
        cls = np.asarray(highway_class, dtype=np.int64)
        maxspeed = np.asarray(maxspeed_kmh, dtype=np.float64)
        flag = np.asarray(flags, dtype=np.uint8)
        u = np.asarray(u, dtype=np.int32)
        v = np.asarray(v, dtype=np.int32)

        self._graphs: dict[str, csr_matrix] = {}
        started = time.perf_counter()
        for mode in MODES:
            secs = self._edge_seconds(mode, length, cls, maxspeed)
            fwd_bit, bwd_bit = MODE_BITS[mode]
            fwd = (flag & fwd_bit) != 0
            bwd = (flag & bwd_bit) != 0
            rows = np.concatenate([u[fwd], v[bwd]])
            colz = np.concatenate([v[fwd], u[bwd]])
            data = np.concatenate([secs[fwd], secs[bwd]]).astype(np.float32)
            del secs, fwd, bwd
            rows, colz, data = _dedupe_min(rows, colz, data, self.num_nodes)
            self._graphs[mode] = csr_matrix(
                (data, (rows, colz)), shape=(self.num_nodes, self.num_nodes)
            )
            del rows, colz, data
        logger.info("street CSR matrices built in %.1f s", time.perf_counter() - started)

        started = time.perf_counter()
        self._snap_nodes: dict[str, np.ndarray] = {}
        self._snap_grids: dict[str, _SnapGrid | None] = {}
        for mode in MODES:
            mask_source = "precomputed"
            if snap_masks is not None and mode in snap_masks:
                mask = np.asarray(snap_masks[mode], dtype=bool)
                node_idx = np.flatnonzero(mask)
            else:
                mask_source = "runtime fallback"
                node_idx = self._giant_scc_nodes(mode)
            self._snap_nodes[mode] = node_idx
            grid = _SnapGrid(node_idx, self._xs, self._ys) if len(node_idx) else None
            self._snap_grids[mode] = grid
            logger.info(
                "snap nodes %s: %d (%s)", mode, len(node_idx), mask_source
            )

        # Car snapping prefers public roads: the nearest usable node in a
        # field is often deep in a farm/service web whose only escape is
        # kilometres of 15 km/h roads — technically connected, practically
        # useless as a start. Fall back to the full car grid when no public
        # road is within MAX_SNAP_M.
        if snap_car_public is not None:
            public_nodes = np.flatnonzero(np.asarray(snap_car_public, dtype=bool))
        else:
            public_nodes = self._car_public_nodes_fallback(u, v, cls, flag)
        self._car_public_nodes = public_nodes
        self._car_public_grid = (
            _SnapGrid(public_nodes, self._xs, self._ys) if len(public_nodes) else None
        )
        logger.info(
            "snap masks + grids built in %.1f s (car public nodes: %d)",
            time.perf_counter() - started,
            len(public_nodes),
        )

    def _giant_scc_nodes(self, mode: str) -> np.ndarray:
        """Nodes of the giant strongly-connected component of one mode graph.

        Snap only onto the giant SCC — a stub that is only weakly connected
        (one-way in, no way out) would still silently yield a tiny isochrone.
        """
        graph = self._graphs[mode]
        usable = np.zeros(self.num_nodes, dtype=bool)
        usable[graph.indices] = True
        usable |= np.diff(graph.indptr) > 0
        node_idx = np.flatnonzero(usable)
        if len(node_idx) == 0:
            return node_idx
        _, labels = connected_components(graph, directed=True, connection="strong")
        usable_labels = labels[node_idx]
        giant = int(np.bincount(usable_labels).argmax())
        return node_idx[usable_labels == giant]

    def _car_public_nodes_fallback(
        self, u: np.ndarray, v: np.ndarray, cls: np.ndarray, flag: np.ndarray
    ) -> np.ndarray:
        """Car snap nodes touching ≥ 1 non-service car edge (runtime path)."""
        car_nodes = self._snap_nodes["car"]
        if len(car_nodes) == 0:
            return car_nodes
        fwd_bit, bwd_bit = MODE_BITS["car"]
        car_edge = (flag & (fwd_bit | bwd_bit)) != 0
        public_edge = car_edge & (cls != _SERVICE_CLASS)
        public = np.zeros(self.num_nodes, dtype=bool)
        public[u[public_edge]] = True
        public[v[public_edge]] = True
        return car_nodes[public[car_nodes]]

    @staticmethod
    def _edge_seconds(
        mode: str, length: np.ndarray, cls: np.ndarray, maxspeed: np.ndarray
    ) -> np.ndarray:
        """Travel time per stored edge row for one mode (inf where unusable)."""
        if mode == "car":
            default = _CAR_DEFAULT_TABLE[cls]
            tagged = np.minimum(CAR_MAXSPEED_FACTOR * maxspeed, CAR_SPEED_CAP_KMH)
            kmh = np.where(maxspeed > 0, tagged, default)
        elif mode == "bike":
            kmh = _BIKE_TABLE[cls]
        else:
            kmh = np.full(len(cls), FOOT_KMH, dtype=np.float64)
        mps = kmh / 3.6
        with np.errstate(divide="ignore"):
            return np.where(mps > 0, length / np.maximum(mps, 1e-9), np.inf)

    @classmethod
    def from_npz(cls, path: str) -> "StreetRouter":
        started = time.perf_counter()
        with np.load(path) as data:
            snap_masks: dict[str, np.ndarray] | None = None
            mask_keys = [f"snap_ok_{mode}" for mode in MODES]
            if all(key in data.files for key in mask_keys):
                snap_masks = {mode: data[f"snap_ok_{mode}"] for mode in MODES}
            snap_car_public = (
                data["snap_car_public"]
                if "snap_car_public" in data.files
                else None
            )
            arrays = (
                data["lats"],
                data["lons"],
                data["u"],
                data["v"],
                data["length_m"],
                data["highway_class"],
                data["maxspeed_kmh"],
                data["flags"],
            )
        logger.info(
            "npz %s read in %.1f s (masks %s)",
            path,
            time.perf_counter() - started,
            "present" if snap_masks is not None else "absent → runtime fallback",
        )
        return cls(*arrays, snap_masks=snap_masks, snap_car_public=snap_car_public)

    def mode_stats(self) -> dict[str, int]:
        return {mode: int(self._graphs[mode].nnz) for mode in MODES}

    def _snap(self, mode: str, lat: float, lon: float) -> tuple[int, float]:
        """(nearest usable node, distance m); OriginTooFarError beyond 2 km."""
        x = lon * self._m_per_deg_lon
        y = lat * M_PER_DEG_LAT
        if mode == "car" and self._car_public_grid is not None:
            found = self._car_public_grid.query(x, y)
            if found is not None:
                return found
        grid = self._snap_grids[mode]
        if grid is None:
            raise OriginTooFarError(f"no nodes usable by mode {mode!r}")
        found = grid.query(x, y)
        if found is None:
            raise OriginTooFarError(
                f"no {mode} node within {MAX_SNAP_M:.0f} m of the origin"
            )
        return found

    def reachable_costs(
        self, mode: str, lat: float, lon: float, budget_s: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """(node_indices, cost_seconds) of nodes reachable within budget."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        if not 0 < budget_s <= MAX_BUDGET_S:
            raise ValueError(f"budget_s must be in (0, {MAX_BUDGET_S}], got {budget_s}")
        source, _ = self._snap(mode, lat, lon)
        costs = dijkstra(
            self._graphs[mode], directed=True, indices=source, limit=budget_s
        )
        reached = np.flatnonzero(costs <= budget_s)
        return reached, costs[reached]

    def _grid_circles(
        self, mode: str, reached: np.ndarray, remaining: np.ndarray
    ) -> list[tuple[float, float, float]]:
        """Downsample reached nodes on a grid → (lat, lon, radius_m) circles."""
        xs = self._xs[reached]
        ys = self._ys[reached]
        width = float(xs.max() - xs.min())
        height = float(ys.max() - ys.min())
        cell_m = max(
            MIN_CELL_M[mode], math.ceil(math.sqrt(width * height / MAX_CIRCLES))
        )
        ix = np.floor(xs / cell_m).astype(np.int64)
        iy = np.floor(ys / cell_m).astype(np.int64)
        cell_key = ix * 2**32 + iy
        order = np.lexsort((-remaining, cell_key))
        keys_sorted = cell_key[order]
        first = np.empty(len(order), dtype=bool)
        first[:1] = True
        first[1:] = keys_sorted[1:] != keys_sorted[:-1]
        chosen = order[first]
        if len(chosen) > MAX_CIRCLES:  # hard bound (bbox estimate can overshoot)
            chosen = chosen[np.argsort(-remaining[chosen])[:MAX_CIRCLES]]
        radius = np.clip(
            remaining[chosen] * OFFROAD_MPS[mode],
            cell_m * RADIUS_MIN_CELL_FACTOR,
            cell_m * RADIUS_MAX_CELL_FACTOR,
        )
        node_idx = reached[chosen]
        return list(
            zip(
                self._lats[node_idx].tolist(),
                self._lons[node_idx].tolist(),
                radius.tolist(),
            )
        )

    @staticmethod
    def _origin_circle(
        lat: float, lon: float, budget_s: float, snap_dist_m: float
    ) -> tuple[float, float, float]:
        """Small disc bridging the gap between the origin and the network.

        Sized by the snap distance (with the walking detour factor), not by
        the whole budget — a budget-sized crow-fly disc would dominate small
        isochrones and inflate their area.
        """
        walk_reach_m = budget_s * WALK_SPEED_MPS / WALK_DETOUR
        radius = min(max(ORIGIN_MIN_RADIUS_M, WALK_DETOUR * snap_dist_m), walk_reach_m)
        return (lat, lon, radius)

    def isochrone(
        self, mode: str, lat: float, lon: float, budget_s: float
    ) -> dict[str, Any]:
        """GeoJSON Feature of the area reachable within ``budget_s`` seconds."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        started = time.perf_counter()
        _, snap_dist_m = self._snap(mode, lat, lon)
        reached, costs = self.reachable_costs(mode, lat, lon, budget_s)
        remaining = budget_s - costs
        circles = self._grid_circles(mode, reached, remaining)
        circles.append(self._origin_circle(lat, lon, budget_s, snap_dist_m))
        feature = circles_to_geojson_feature(
            circles, {"mode": mode, "minutes": int(round(budget_s / 60))}
        )
        logger.debug(
            "isochrone %s %.0f s: %d nodes, %d circles, %.0f ms",
            mode,
            budget_s,
            len(reached),
            len(circles),
            (time.perf_counter() - started) * 1000,
        )
        return feature


def load_router(path: str) -> StreetRouter:
    """Load the router from an npz file; OSError when the file is missing."""
    started = time.perf_counter()
    router = StreetRouter.from_npz(path)
    logger.info(
        "street graph loaded in %.1f s: %d nodes, %d edge rows, directed %s",
        time.perf_counter() - started,
        router.num_nodes,
        router.num_edges,
        router.mode_stats(),
    )
    return router
