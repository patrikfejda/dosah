"""Tests for server.gtfs — synthetic GTFS zip, no dependency on the real feed.

Fixture services (calendar valid 2026-09-01 → 2026-12-31):
  * ``Prac.dny_0``  Mon–Fri, removed on 2026-09-17 via calendar_dates
  * ``Vikend_0``    Sat–Sun
  * ``Special_0``   only via calendar_dates add on 2026-09-16 (a Wednesday)

Trips:
  * T1, T2: S1→S2→S3 (same pattern; T2 departs earlier but is listed later);
    T1 has shuffled non-contiguous stop_sequence values and a row referencing
    the station ST9 (must be dropped)
  * T3: night trip S1→S2 with times past 24:00:00; S2 arrival empty
  * TS: S1→S3 (Special_0)
  * TW: S3→S1 (weekend)
  * TB: row with both times empty → whole trip dropped
"""

from __future__ import annotations

import datetime
import zipfile
from pathlib import Path

import pytest

from server.gtfs import load_network, parse_gtfs_time
from server.model import Pattern

WEDNESDAY = datetime.date(2026, 9, 16)
THURSDAY_REMOVED = datetime.date(2026, 9, 17)
SATURDAY = datetime.date(2026, 9, 19)

FILES = {
    "stops.txt": [
        "stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,zone_id,location_type,parent_station",
        'S1,,"Stop, One",,48.10,17.10,,0,',
        "S2,,Stop Two,,48.11,17.10,,,",
        "S3,,Stop Three,,48.12,17.10,,0,",
        "ST9,,Station,,48.13,17.10,,1,",
        "SBAD,,No Coords,,,,,0,",
    ],
    "routes.txt": [
        "route_id,agency_id,route_short_name,route_long_name,route_type",
        "R1,,1,Test Line,0",
    ],
    "trips.txt": [
        "route_id,service_id,trip_id,trip_headsign",
        "R1,Prac.dny_0,T1,Head",
        "R1,Prac.dny_0,T2,Head",
        "R1,Prac.dny_0,T3,Night",
        "R1,Prac.dny_0,TB,Broken",
        "R1,Special_0,TS,Special",
        "R1,Vikend_0,TW,Weekend",
    ],
    "stop_times.txt": [
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
        "stop_headsign,pickup_type,drop_off_type,shape_dist_traveled,timepoint",
        # T1 shuffled, non-contiguous sequences + a station row to drop
        "T1,08:10:00,08:10:00,S3,100,,,,,",
        "T1,08:00:00,08:00:00,S1,5,,,,,",
        "T1,08:12:00,08:12:00,ST9,150,,,,,",
        "T1,08:05:00,08:05:00,S2,20,,,,,",
        # T2 same stop sequence, earlier departure, listed after T1
        "T2,07:30:00,07:30:00,S1,1,,,,,",
        "T2,07:35:00,07:35:00,S2,2,,,,,",
        "T2,07:40:00,07:40:00,S3,3,,,,,",
        # T3 night trip past 24:00, S2 arrival empty (must fall back to dep)
        "T3,24:10:00,24:10:00,S1,1,,,,,",
        "T3,,24:20:00,S2,2,,,,,",
        # TB has a row with both times empty → drop the whole trip
        "TB,09:00:00,09:00:00,S1,1,,,,,",
        "TB,,,S2,2,,,,,",
        # TS special service, skips S2
        "TS,10:00:00,10:00:00,S1,1,,,,,",
        "TS,10:08:00,10:08:00,S3,2,,,,,",
        # TW weekend, reverse direction
        "TW,11:00:00,11:00:00,S3,1,,,,,",
        "TW,11:15:00,11:15:00,S1,2,,,,,",
    ],
    "calendar.txt": [
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date",
        "Prac.dny_0,1,1,1,1,1,0,0,20260901,20261231",
        "Vikend_0,0,0,0,0,0,1,1,20260901,20261231",
    ],
    "calendar_dates.txt": [
        "service_id,date,exception_type",
        "Prac.dny_0,20260917,2",
        "Special_0,20260916,1",
    ],
}


@pytest.fixture()
def gtfs_zip(tmp_path: Path) -> str:
    path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, lines in FILES.items():
            zf.writestr(name, "\r\n".join(lines) + "\r\n")
    return str(path)


def pattern_by_stops(patterns: list[Pattern], stop_ids: list[str]) -> Pattern:
    matches = [p for p in patterns if p.stop_ids == stop_ids]
    assert len(matches) == 1, f"expected exactly one pattern {stop_ids}"
    return matches[0]


def test_parse_gtfs_time_past_24h() -> None:
    assert parse_gtfs_time("24:10:00") == 24 * 3600 + 600
    assert parse_gtfs_time("08:05:30") == 8 * 3600 + 5 * 60 + 30
    assert parse_gtfs_time("") is None


def test_weekday_load_filters_stops_and_services(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    # station ST9 and coordinate-less SBAD excluded
    assert set(net.stop_coords) == {"S1", "S2", "S3"}
    # active: Prac.dny_0 (weekday) + Special_0 (added); TB dropped (empty times)
    assert len(net.patterns) == 3
    total_trips = sum(len(p.trip_departures) for p in net.patterns)
    assert total_trips == 4  # T1, T2, T3, TS
    assert net.service_date == "20260916"


def test_stop_sequence_sorted_and_station_row_dropped(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    pattern = pattern_by_stops(net.patterns, ["S1", "S2", "S3"])
    for departures in pattern.trip_departures:
        assert departures == sorted(departures)
    # T1's ST9 row was dropped, so both trips share the 3-stop pattern
    assert len(pattern.trip_departures) == 2


def test_pattern_groups_trips_sorted_by_first_departure(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    pattern = pattern_by_stops(net.patterns, ["S1", "S2", "S3"])
    first_departures = [deps[0] for deps in pattern.trip_departures]
    # T2 (07:30) sorts before T1 (08:00) despite file order
    assert first_departures == [27000, 28800]


def test_night_times_past_24h_and_arrival_fallback(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    pattern = pattern_by_stops(net.patterns, ["S1", "S2"])
    assert pattern.trip_departures == [[87000, 87600]]
    # empty arrival at S2 falls back to the departure value
    assert pattern.trip_arrivals == [[87000, 87600]]


def test_calendar_dates_add(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    pattern = pattern_by_stops(net.patterns, ["S1", "S3"])  # TS via Special_0
    assert pattern.trip_departures == [[36000, 36480]]


def test_calendar_dates_remove_yields_no_service(gtfs_zip: str) -> None:
    # Thursday: Prac.dny_0 removed by exception, nothing else runs
    with pytest.raises(ValueError, match="2026-09-17"):
        load_network(gtfs_zip, THURSDAY_REMOVED)


def test_weekend_service(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, SATURDAY)
    assert len(net.patterns) == 1
    pattern = pattern_by_stops(net.patterns, ["S3", "S1"])
    assert pattern.trip_departures == [[39600, 40500]]


def test_stop_patterns_index_consistent(gtfs_zip: str) -> None:
    net = load_network(gtfs_zip, WEDNESDAY)
    for stop_id, refs in net.stop_patterns.items():
        for p_idx, s_idx in refs:
            assert net.patterns[p_idx].stop_ids[s_idx] == stop_id
