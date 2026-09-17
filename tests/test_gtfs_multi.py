"""Tests for gtfs.load_combined_network — multi-feed merge, two service days,
route_type filtering (rail-only feeds).

2026-09-16 is a Wednesday.
"""

from __future__ import annotations

import datetime
import zipfile
from pathlib import Path

import pytest

from server.gtfs import FeedSpec, load_combined_network

WED = datetime.date(2026, 9, 16)

DAY_S = 86_400


def write_feed(
    path: Path,
    *,
    weekdays: dict[str, str],
    routes: list[tuple[str, str]],  # (route_id, route_type)
    trips: list[tuple[str, str, str]],  # (route_id, service_id, trip_id)
    stop_times: list[tuple[str, str, str, str, str]],  # trip, arr, dep, stop, seq
    stops: list[tuple[str, str, str]],  # (stop_id, lat, lon)
    start_date: str = "20260915",
    end_date: str = "20261231",
) -> str:
    """Write a minimal GTFS zip; ``weekdays`` maps service_id → 'MTWTFSS' flags."""
    with zipfile.ZipFile(path, "w") as zf:
        lines = ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date"]
        for service_id, flags in weekdays.items():
            lines.append(f"{service_id},{','.join(flags)},{start_date},{end_date}")
        zf.writestr("calendar.txt", "\n".join(lines) + "\n")
        zf.writestr(
            "routes.txt",
            "route_id,route_short_name,route_type\n"
            + "".join(f"{r},{r},{t}\n" for r, t in routes),
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id\n"
            + "".join(f"{r},{s},{t}\n" for r, s, t in trips),
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            + "".join(f"{t},{a},{d},{s},{q}\n" for t, a, d, s, q in stop_times),
        )
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
            + "".join(f"{s},{s},{lat},{lon},0\n" for s, lat, lon in stops),
        )
    return str(path)


def simple_feed(path: Path, *, flags: str = "0010000") -> str:
    """One route, one Wednesday trip S1 08:00 → S2 08:10."""
    return write_feed(
        path,
        weekdays={"svc": flags},
        routes=[("r1", "3")],
        trips=[("r1", "svc", "t1")],
        stop_times=[
            ("t1", "08:00:00", "08:00:00", "S1", "1"),
            ("t1", "08:10:00", "08:10:00", "S2", "2"),
        ],
        stops=[("S1", "48.10", "17.10"), ("S2", "48.12", "17.10")],
    )


def test_two_feeds_merge_with_prefixes(tmp_path: Path) -> None:
    feed_a = simple_feed(tmp_path / "a.zip")
    feed_b = write_feed(
        tmp_path / "b.zip",
        weekdays={"svc": "0010000"},
        routes=[("rx", "2")],
        # same raw stop_id "S1" as feed A — prefixes must keep them apart
        trips=[("rx", "svc", "t1")],
        stop_times=[
            ("t1", "09:00:00", "09:00:00", "S1", "1"),
            ("t1", "09:30:00", "09:30:00", "S9", "2"),
        ],
        stops=[("S1", "48.20", "17.20"), ("S9", "48.30", "17.30")],
    )
    net = load_combined_network(
        [FeedSpec(feed_a, "dpb:"), FeedSpec(feed_b, "zsr:")], WED, days=1
    )
    assert set(net.stop_coords) == {"dpb:S1", "dpb:S2", "zsr:S1", "zsr:S9"}
    assert net.stop_coords["dpb:S1"] == (48.10, 17.10)
    assert net.stop_coords["zsr:S1"] == (48.20, 17.20)
    all_pattern_stops = {s for p in net.patterns for s in p.stop_ids}
    assert all_pattern_stops == set(net.stop_coords)


def test_second_day_trips_shifted_by_24h(tmp_path: Path) -> None:
    # service runs Wed AND Thu; loading Wed with days=2 must include the
    # Thursday run shifted +86400 s
    feed = simple_feed(tmp_path / "a.zip", flags="0011000")
    net = load_combined_network([FeedSpec(feed, "")], WED, days=2)
    departures = sorted(
        dep for p in net.patterns for trip in p.trip_departures for dep in trip[:1]
    )
    assert departures == [8 * 3600, 8 * 3600 + DAY_S]


def test_single_day_by_default_excludes_tomorrow(tmp_path: Path) -> None:
    feed = simple_feed(tmp_path / "a.zip", flags="0011000")
    net = load_combined_network([FeedSpec(feed, "")], WED, days=1)
    departures = [dep for p in net.patterns for trip in p.trip_departures for dep in trip[:1]]
    assert departures == [8 * 3600]


def test_route_type_filter_keeps_only_rail(tmp_path: Path) -> None:
    feed = write_feed(
        tmp_path / "mixed.zip",
        weekdays={"svc": "0010000"},
        routes=[("bus1", "3"), ("rail1", "2")],
        trips=[("bus1", "svc", "tb"), ("rail1", "svc", "tr")],
        stop_times=[
            ("tb", "08:00:00", "08:00:00", "S1", "1"),
            ("tb", "08:10:00", "08:10:00", "S2", "2"),
            ("tr", "09:00:00", "09:00:00", "S1", "1"),
            ("tr", "09:20:00", "09:20:00", "S2", "2"),
        ],
        stops=[("S1", "48.10", "17.10"), ("S2", "48.12", "17.10")],
    )
    net = load_combined_network(
        [FeedSpec(feed, "zsr:", route_types={2})], WED, days=1
    )
    departures = [dep for p in net.patterns for trip in p.trip_departures for dep in trip[:1]]
    assert departures == [9 * 3600]  # bus trip filtered out


def test_no_service_tomorrow_is_fine(tmp_path: Path) -> None:
    # feed valid only on the requested Wednesday; days=2 must not raise
    feed = simple_feed(tmp_path / "a.zip")
    net = load_combined_network(
        [FeedSpec(feed, "")], WED, days=2
    )
    assert len(net.patterns) == 1


def test_no_service_today_raises(tmp_path: Path) -> None:
    feed = simple_feed(tmp_path / "a.zip", flags="0000010")  # Saturdays only
    with pytest.raises(ValueError):
        load_combined_network([FeedSpec(feed, "")], WED, days=2)


def test_one_broken_feed_does_not_kill_the_other(tmp_path: Path) -> None:
    feed_a = simple_feed(tmp_path / "a.zip")
    missing = str(tmp_path / "missing.zip")
    net = load_combined_network(
        [FeedSpec(feed_a, "dpb:"), FeedSpec(missing, "zsr:")], WED, days=1
    )
    assert set(net.stop_coords) == {"dpb:S1", "dpb:S2"}
