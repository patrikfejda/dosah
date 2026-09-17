#!/usr/bin/env python
"""One-off preprocessing: OSM PBFs → compact street graph (data/streets.npz).

Usage: .venv/bin/python scripts/build_graph.py [pbf_path ...] [-o out_path]
Without arguments every ``data/*-latest.osm.pbf`` present is merged.

One pyosmium pass per file with a flex_mem node-location cache; node OSM ids
are compacted to 0..N-1 across all files (shared border nodes dedupe
naturally, duplicated border-crossing ways dedupe by way id). Each
consecutive node pair along a routable way becomes one undirected edge row;
direction lives in the flags bitmask (see server/streets_model.py).

Scope trimming keeps the file small: everything is clipped to GLOBAL_BBOX,
and foot/bike flags are kept only on edges whose BOTH endpoints lie inside
FOOT_BIKE_BBOX (SK + Vienna/Brno/Budapest fringe) — only car keeps the whole
region.

Snap masks are precomputed here so the server never has to run
connected_components over ~90 M nodes at startup: per mode the giant
strongly-connected component (snap_ok_walk/bike/car) plus snap_car_public
(car SCC nodes touching ≥ 1 non-service car edge).
"""

from __future__ import annotations

import sys
import time
from array import array
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

if TYPE_CHECKING:
    from osmium.osm import Way

import osmium

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.streets_model import (  # noqa: E402
    CAR_BWD,
    CAR_FWD,
    CLASS_ID,
    FOOT_BIKE_BBOX,
    GLOBAL_BBOX,
    MODE_BITS,
    NUM_CLASSES,
    classify_way,
    in_bbox,
    trim_foot_bike_flags,
)

DEFAULT_OUT = PROJECT_ROOT / "data" / "streets.npz"
MAXSPEED_STORE_CAP = 65_535  # uint16 clamp
PROGRESS_EVERY_WAYS = 200_000

_SERVICE_CLASS = CLASS_ID["service"]


class GraphBuilder(osmium.SimpleHandler):
    """Collects compacted nodes + edge rows from routable highway ways."""

    def __init__(self) -> None:
        super().__init__()
        self.node_index: dict[int, int] = {}
        self.seen_way_ids: set[int] = set()  # dedupe border-crossing ways
        self.lats = array("d")
        self.lons = array("d")
        self.node_in_foot_bike = array("b")  # endpoint inside FOOT_BIKE_BBOX
        self.edge_u = array("i")
        self.edge_v = array("i")
        self.edge_class = array("B")
        self.edge_maxspeed = array("H")
        self.edge_flags = array("B")
        self.ways_seen = 0
        self.ways_routable = 0
        self.ways_deduped = 0
        self.segments_skipped = 0  # segments with missing node locations
        self.edges_declassed = 0  # edges kept car-only outside FOOT_BIKE_BBOX

    def way(self, w: "Way") -> None:
        self.ways_seen += 1
        if self.ways_seen % PROGRESS_EVERY_WAYS == 0:
            print(
                f"  ... {self.ways_seen:,} ways scanned, "
                f"{len(self.edge_u):,} edges, {len(self.lats):,} nodes",
                flush=True,
            )
        if "highway" not in w.tags:
            return
        result = classify_way({t.k: t.v for t in w.tags})
        if result is None:
            return
        if w.id in self.seen_way_ids:
            self.ways_deduped += 1
            return
        self.seen_way_ids.add(w.id)
        flags, cls, maxspeed = result
        maxspeed = min(maxspeed, MAXSPEED_STORE_CAP)
        self.ways_routable += 1
        min_lat, min_lon, max_lat, max_lon = GLOBAL_BBOX
        prev_idx = -1
        for node in w.nodes:
            loc = node.location
            if not loc.valid() or not (
                min_lat <= loc.lat <= max_lat and min_lon <= loc.lon <= max_lon
            ):
                if prev_idx >= 0:
                    self.segments_skipped += 1
                prev_idx = -1
                continue
            idx = self.node_index.get(node.ref)
            if idx is None:
                idx = len(self.lats)
                self.node_index[node.ref] = idx
                self.lats.append(loc.lat)
                self.lons.append(loc.lon)
                self.node_in_foot_bike.append(
                    in_bbox(loc.lat, loc.lon, FOOT_BIKE_BBOX)
                )
            if prev_idx >= 0 and prev_idx != idx:
                edge_flags = trim_foot_bike_flags(
                    flags,
                    bool(self.node_in_foot_bike[prev_idx]),
                    bool(self.node_in_foot_bike[idx]),
                )
                if edge_flags != flags:
                    self.edges_declassed += 1
                if edge_flags:
                    self.edge_u.append(prev_idx)
                    self.edge_v.append(idx)
                    self.edge_class.append(cls)
                    self.edge_maxspeed.append(maxspeed)
                    self.edge_flags.append(edge_flags)
            prev_idx = idx


def _haversine_m(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    earth_radius_m = 6_371_000.0
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = phi2 - phi1
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlmb / 2) ** 2
    return 2 * earth_radius_m * np.arcsin(np.sqrt(a))


def compute_snap_masks(
    u: np.ndarray, v: np.ndarray, flags: np.ndarray, num_nodes: int
) -> dict[str, np.ndarray]:
    """Giant strongly-connected component membership per mode (bool masks).

    Mirrors the directed graph server/streets.py builds (fwd/bwd flag bits →
    directed edges); connectivity only needs topology, so edge data is bool.
    """
    masks: dict[str, np.ndarray] = {}
    for mode, (fwd_bit, bwd_bit) in MODE_BITS.items():
        started = time.perf_counter()
        fwd = (flags & fwd_bit) != 0
        bwd = (flags & bwd_bit) != 0
        rows = np.concatenate([u[fwd], v[bwd]])
        cols = np.concatenate([v[fwd], u[bwd]])
        del fwd, bwd
        mask = np.zeros(num_nodes, dtype=bool)
        if len(rows) == 0:
            masks[mode] = mask
            print(f"  snap_ok_{mode}: 0 nodes (no edges)", flush=True)
            continue
        usable = np.zeros(num_nodes, dtype=bool)
        usable[rows] = True
        usable[cols] = True
        graph = csr_matrix(
            (np.ones(len(rows), dtype=bool), (rows, cols)),
            shape=(num_nodes, num_nodes),
        )
        del rows, cols
        _, labels = connected_components(graph, directed=True, connection="strong")
        del graph
        node_idx = np.flatnonzero(usable)
        del usable
        usable_labels = labels[node_idx]
        del labels
        giant = int(np.bincount(usable_labels).argmax())
        mask[node_idx[usable_labels == giant]] = True
        del node_idx, usable_labels
        masks[mode] = mask
        print(
            f"  snap_ok_{mode}: {int(mask.sum()):,}/{num_nodes:,} nodes "
            f"in {time.perf_counter() - started:.1f} s",
            flush=True,
        )
    return masks


def compute_car_public_mask(
    u: np.ndarray,
    v: np.ndarray,
    flags: np.ndarray,
    highway_class: np.ndarray,
    snap_ok_car: np.ndarray,
) -> np.ndarray:
    """snap_ok_car AND node touches ≥ 1 car edge with class != service."""
    started = time.perf_counter()
    car_edge = (flags & (CAR_FWD | CAR_BWD)) != 0
    public_edge = car_edge & (highway_class != _SERVICE_CLASS)
    del car_edge
    public = np.zeros(len(snap_ok_car), dtype=bool)
    public[u[public_edge]] = True
    public[v[public_edge]] = True
    del public_edge
    mask = snap_ok_car & public
    print(
        f"  snap_car_public: {int(mask.sum()):,}/{int(snap_ok_car.sum()):,} "
        f"car snap nodes in {time.perf_counter() - started:.1f} s",
        flush=True,
    )
    return mask


def build(pbf_paths: list[Path], out_path: Path) -> None:
    started = time.perf_counter()
    builder = GraphBuilder()
    for pbf_path in pbf_paths:
        print(f"Parsing {pbf_path.name} ...", flush=True)
        file_started = time.perf_counter()
        builder.apply_file(str(pbf_path), locations=True, idx="flex_mem")
        print(
            f"  done in {time.perf_counter() - file_started:.1f} s "
            f"({len(builder.lats):,} nodes, {len(builder.edge_u):,} edges so far)",
            flush=True,
        )
    parse_s = time.perf_counter() - started
    print(f"Parse done in {parse_s:.1f} s", flush=True)

    lats = np.frombuffer(builder.lats, dtype=np.float64)
    lons = np.frombuffer(builder.lons, dtype=np.float64)
    u = np.frombuffer(builder.edge_u, dtype=np.int32)
    v = np.frombuffer(builder.edge_v, dtype=np.int32)
    highway_class = np.frombuffer(builder.edge_class, dtype=np.uint8)
    maxspeed_kmh = np.frombuffer(builder.edge_maxspeed, dtype=np.uint16)
    flags = np.frombuffer(builder.edge_flags, dtype=np.uint8)
    # Release the builder's Python-side lookup structures before the numpy
    # post-processing — the node dict alone is gigabytes at ~90 M nodes.
    builder.node_index.clear()
    builder.seen_way_ids.clear()

    # Drop orphan nodes (ways whose every edge lost all mode flags) and remap.
    used = np.zeros(len(lats), dtype=bool)
    used[u] = True
    used[v] = True
    new_index = np.cumsum(used, dtype=np.int64) - 1
    orphans = int(len(lats) - used.sum())
    lats, lons = lats[used], lons[used]
    u = new_index[u].astype(np.int32)
    v = new_index[v].astype(np.int32)
    del used, new_index
    print(f"Orphan nodes dropped: {orphans:,}", flush=True)

    length_m = _haversine_m(lats[u], lons[u], lats[v], lons[v]).astype(np.float32)

    print("Precomputing snap masks (giant SCC per mode) ...", flush=True)
    masks_started = time.perf_counter()
    snap_masks = compute_snap_masks(u, v, flags, len(lats))
    snap_car_public = compute_car_public_mask(
        u, v, flags, highway_class, snap_masks["car"]
    )
    masks_s = time.perf_counter() - masks_started
    print(f"Snap masks done in {masks_s:.1f} s", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_started = time.perf_counter()
    np.savez_compressed(
        out_path,
        lats=lats,
        lons=lons,
        u=u,
        v=v,
        length_m=length_m,
        highway_class=highway_class,
        maxspeed_kmh=maxspeed_kmh,
        flags=flags,
        snap_ok_walk=snap_masks["walk"],
        snap_ok_bike=snap_masks["bike"],
        snap_ok_car=snap_masks["car"],
        snap_car_public=snap_car_public,
    )
    save_s = time.perf_counter() - save_started
    total_s = time.perf_counter() - started

    class_names = {cls: name for name, cls in CLASS_ID.items()}
    counts = np.bincount(highway_class, minlength=NUM_CLASSES)
    print(f"\nWays scanned:    {builder.ways_seen:,}")
    print(f"Ways routable:   {builder.ways_routable:,}")
    print(f"Ways deduped (cross-extract): {builder.ways_deduped:,}")
    print(f"Edges foot/bike-trimmed outside FOOT_BIKE_BBOX: {builder.edges_declassed:,}")
    print(f"Segments skipped (missing/out-of-bbox locations): {builder.segments_skipped:,}")
    print(f"Nodes:           {len(lats):,}")
    print(f"Edge rows:       {len(u):,} (undirected storage)")
    for mode in ("walk", "bike", "car"):
        print(f"snap_ok_{mode}:{'':<{6 - len(mode)}} {int(snap_masks[mode].sum()):>12,}")
    print(f"snap_car_public: {int(snap_car_public.sum()):>12,}")
    print("Per-class edge counts:")
    for cls in range(NUM_CLASSES):
        if counts[cls]:
            print(f"  {class_names[cls]:<16} {counts[cls]:>9,}")
    print(
        f"Build time:      {total_s:.1f} s "
        f"(parse {parse_s:.1f} s, masks {masks_s:.1f} s, save {save_s:.1f} s)"
    )
    print(f"Output:          {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str]) -> int:
    args = argv[1:]
    out_path = DEFAULT_OUT
    if "-o" in args:
        flag_at = args.index("-o")
        if flag_at + 1 >= len(args):
            print("error: -o requires a path", file=sys.stderr)
            return 1
        out_path = Path(args[flag_at + 1])
        args = args[:flag_at] + args[flag_at + 2 :]

    if args:
        pbf_paths = [Path(a) for a in args]
    else:
        pbf_paths = sorted((PROJECT_ROOT / "data").glob("*-latest.osm.pbf"))
    if not pbf_paths:
        print("error: no PBF files given or found in data/", file=sys.stderr)
        return 1
    missing = [p for p in pbf_paths if not p.is_file()]
    if missing:
        print(f"error: PBF file(s) not found: {missing}", file=sys.stderr)
        return 1
    build(pbf_paths, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
