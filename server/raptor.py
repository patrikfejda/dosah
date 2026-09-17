"""RAPTOR-style transit reachability.

Computes the earliest arrival time (seconds since service-day midnight) at
every stop reachable from an origin point within a time budget, walking to
and between stops and riding at most ``max_rounds`` vehicles.
"""

from __future__ import annotations

from bisect import bisect_left

from server.model import Pattern, TransitNetwork

_INF = float("inf")


def reachable_stops(
    net: TransitNetwork,
    origin_lat: float,
    origin_lon: float,
    depart_s: int,
    budget_s: int,
    *,
    max_rounds: int = 4,
    walk_speed: float = 1.33,
    detour: float = 1.3,
    min_transfer_s: int = 60,
    transfer_radius_m: float = 300.0,
) -> dict[str, int]:
    """Earliest arrival per stop, all within [depart_s, depart_s + budget_s].

    ``max_rounds`` bounds the number of vehicle rides (explicit bound;
    max_rounds=1 means direct rides only). Walk arrivals are boarding-ready
    as-is; ``min_transfer_s`` applies only after alighting a vehicle.
    """
    if budget_s <= 0:
        raise ValueError("budget_s must be positive")

    limit = depart_s + budget_s
    arrivals: dict[str, int] = {}
    by_vehicle: set[str] = set()

    # Access: walk from the origin to every stop within budget reach.
    access_radius_m = budget_s * walk_speed / detour
    improved: set[str] = set()
    for stop_id, dist_m in net.stops_within(origin_lat, origin_lon, access_radius_m):
        arrival = depart_s + int(dist_m * detour / walk_speed)
        if arrival <= limit:
            arrivals[stop_id] = arrival
            improved.add(stop_id)

    for _ in range(max_rounds):
        if not improved:
            break
        riders = _scan_patterns(net, arrivals, by_vehicle, improved, limit, min_transfer_s)
        for stop_id, arrival in riders.items():
            arrivals[stop_id] = arrival
            by_vehicle.add(stop_id)
        walkers = _relax_transfers(
            net, arrivals, set(riders), limit, walk_speed, detour, transfer_radius_m
        )
        for stop_id, arrival in walkers.items():
            arrivals[stop_id] = arrival
            by_vehicle.discard(stop_id)
        improved = set(riders) | set(walkers)

    return arrivals


def _scan_patterns(
    net: TransitNetwork,
    arrivals: dict[str, int],
    by_vehicle: set[str],
    improved: set[str],
    limit: int,
    min_transfer_s: int,
) -> dict[str, int]:
    """One RAPTOR round: ride every pattern touched by an improved stop."""
    queue: dict[int, int] = {}  # pattern index -> earliest improved stop index
    for stop_id in improved:
        for p_idx, s_idx in net.stop_patterns.get(stop_id, ()):
            current = queue.get(p_idx)
            if current is None or s_idx < current:
                queue[p_idx] = s_idx

    riders: dict[str, int] = {}
    for p_idx, start_idx in queue.items():
        _scan_one_pattern(
            net.patterns[p_idx],
            start_idx,
            arrivals,
            by_vehicle,
            improved,
            limit,
            min_transfer_s,
            riders,
        )
    return riders


def _scan_one_pattern(
    pattern: Pattern,
    start_idx: int,
    arrivals: dict[str, int],
    by_vehicle: set[str],
    improved: set[str],
    limit: int,
    min_transfer_s: int,
    riders: dict[str, int],
) -> None:
    """Standard RAPTOR trip scan along one pattern, updating ``riders``."""
    departures = pattern.trip_departures
    n_trips = len(departures)
    trip: int | None = None  # index of the currently caught trip

    for idx in range(start_idx, len(pattern.stop_ids)):
        stop_id = pattern.stop_ids[idx]

        if trip is not None:
            arrival = pattern.trip_arrivals[trip][idx]
            best = min(arrivals.get(stop_id, _INF), riders.get(stop_id, _INF))
            if arrival <= limit and arrival < best:
                riders[stop_id] = arrival

        if stop_id not in improved:
            continue
        # Boarding uses the previous round's arrival; the transfer buffer
        # applies only when that arrival came from a vehicle.
        ready = arrivals[stop_id] + (min_transfer_s if stop_id in by_vehicle else 0)
        candidate = bisect_left(departures, ready, key=lambda row: row[idx])
        if candidate < n_trips and (trip is None or candidate < trip):
            trip = candidate


def _relax_transfers(
    net: TransitNetwork,
    arrivals: dict[str, int],
    sources: set[str],
    limit: int,
    walk_speed: float,
    detour: float,
    transfer_radius_m: float,
) -> dict[str, int]:
    """Foot transfers from newly improved stops to nearby stops."""
    walkers: dict[str, int] = {}
    for stop_id in sources:
        lat, lon = net.stop_coords[stop_id]
        base = arrivals[stop_id]
        for other_id, dist_m in net.stops_within(lat, lon, transfer_radius_m):
            if other_id == stop_id:
                continue
            arrival = base + int(dist_m * detour / walk_speed)
            best = min(arrivals.get(other_id, _INF), walkers.get(other_id, _INF))
            if arrival <= limit and arrival < best:
                walkers[other_id] = arrival
    return walkers
