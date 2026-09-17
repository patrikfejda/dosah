"""Tests for server.raptor — transit reachability on a synthetic network.

Geometry of the fixture (stops on a north-south line near Bratislava,
0.018° lat ≈ 2004 m apart):

    A (48.100, 17.100)   ── pattern P1: A → B → C
    B (48.118, 17.100)   ── pattern P2: B → D (D is far east of the line)
    C (48.136, 17.100)
    D (48.118, 17.154)   (~4 km east of B)

Walking A→B crow-fly is ~2004 m → with detour 1.3 and speed 1.33 m/s
that is ~1959 s on foot, so within a 1800 s budget B is walk-unreachable.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.model import Pattern, TransitNetwork
from server.raptor import reachable_stops

WALK_SPEED = 1.33
DETOUR = 1.3

A, B, C, D = "A", "B", "C", "D"


def build_network(extra_patterns: list[Pattern] | None = None) -> TransitNetwork:
    # P1 trips: trip0 dep A 600 → arr B 900 (dep 920) → arr C 1200
    #           trip1 dep A 2000 → arr B 2300 (dep 2320) → arr C 2600
    p1 = Pattern(
        stop_ids=[A, B, C],
        trip_departures=[[600, 920, 1200], [2000, 2320, 2600]],
        trip_arrivals=[[600, 900, 1200], [2000, 2300, 2600]],
    )
    # P2 trips from B to D: dep B 950 → arr D 1250; dep B 1000 → arr D 1300
    p2 = Pattern(
        stop_ids=[B, D],
        trip_departures=[[950, 1250], [1000, 1300]],
        trip_arrivals=[[950, 1250], [1000, 1300]],
    )
    patterns = [p1, p2] + (extra_patterns or [])
    stop_patterns: dict[str, list[tuple[int, int]]] = {}
    for p_idx, pattern in enumerate(patterns):
        for s_idx, stop_id in enumerate(pattern.stop_ids):
            stop_patterns.setdefault(stop_id, []).append((p_idx, s_idx))
    return TransitNetwork(
        stop_coords={
            A: (48.100, 17.100),
            B: (48.118, 17.100),
            C: (48.136, 17.100),
            D: (48.118, 17.154),
        },
        patterns=patterns,
        stop_patterns=stop_patterns,
        service_date="20260916",
    )


def reach(
    net: TransitNetwork, depart_s: int, budget_s: int, **kwargs: Any
) -> dict[str, int]:
    return reachable_stops(net, 48.100, 17.100, depart_s, budget_s, **kwargs)


def test_origin_stop_reached_immediately() -> None:
    result = reach(build_network(), depart_s=0, budget_s=300)
    assert result[A] == 0
    # B is ~1959 s away on foot and no trip fits in 300 s
    assert B not in result


def test_ride_beats_walking() -> None:
    result = reach(build_network(), depart_s=0, budget_s=1800)
    assert result[B] == 900  # arrival of trip0, not ~1959 s walk
    assert result[C] == 1200


def test_budget_is_hard_cutoff() -> None:
    result = reach(build_network(), depart_s=0, budget_s=1000)
    assert result[B] == 900
    assert C not in result  # would arrive 1200 > 1000


def test_departure_time_selects_later_trip() -> None:
    # depart at 700: trip0 (dep A 600) is gone, trip1 dep A 2000 → arr B 2300
    result = reach(build_network(), depart_s=700, budget_s=1800)
    assert result[B] == 2300


def test_transfer_respects_min_transfer_time() -> None:
    # arrive B 900 by vehicle; with 60 s buffer the 950 departure to D is
    # missed, the 1000 departure is caught → D at 1300
    result = reach(build_network(), depart_s=0, budget_s=1800, min_transfer_s=60)
    assert result[D] == 1300


def test_transfer_without_buffer_catches_earlier_trip() -> None:
    result = reach(build_network(), depart_s=0, budget_s=1800, min_transfer_s=0)
    assert result[D] == 1250


def test_max_rounds_bounds_transfers() -> None:
    # with a single round (no transfers) D must be unreachable
    result = reach(build_network(), depart_s=0, budget_s=1800, max_rounds=1)
    assert B in result
    assert D not in result


def test_all_arrivals_within_budget_and_after_departure() -> None:
    depart, budget = 300, 1500
    result = reach(build_network(), depart_s=depart, budget_s=budget)
    assert result  # something is reachable
    for arrival in result.values():
        assert depart <= arrival <= depart + budget


def test_invalid_budget_rejected() -> None:
    with pytest.raises(ValueError):
        reach(build_network(), depart_s=0, budget_s=0)
