"""GTFS zips → in-memory :class:`TransitNetwork`.

Supports merging several feeds (city + rail) with stop-id prefixes, loading
two consecutive service days (trips of day N+1 shifted by +24 h so long
after-midnight budgets keep working), and per-feed route_type filtering.

Only stdlib parsing (csv + zipfile). stop_times.txt is streamed row by row;
times above 24:00:00 (night lines) are parsed manually to seconds.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from server.model import Pattern, TransitNetwork

logger = logging.getLogger(__name__)

DAY_S = 86_400

_WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# (stop_sequence, stop_id, arrival_s, departure_s)
_StopTimeRow = tuple[int, str, int, int]


@dataclass(frozen=True)
class FeedSpec:
    """One GTFS zip to merge into the combined network.

    ``prefix`` namespaces stop ids across feeds (e.g. ``"dpb:"``, ``"zsr:"``).
    ``route_types`` restricts trips to those route types (None = all).
    """

    zip_path: str
    prefix: str = ""
    route_types: frozenset[int] | set[int] | None = None


def load_network(zip_path: str, service_date: datetime.date) -> TransitNetwork:
    """Single feed, single service day (backwards-compatible wrapper)."""
    return load_combined_network([FeedSpec(zip_path)], service_date, days=1)


def load_combined_network(
    feeds: list[FeedSpec], service_date: datetime.date, *, days: int = 2
) -> TransitNetwork:
    """Merge feeds into one timetable starting on ``service_date``.

    Loads up to ``days`` consecutive service days; day N trips are shifted by
    N*24 h so budgets crossing midnight see the next morning's departures.
    An unreadable feed is skipped with a warning. Raises ValueError when no
    feed has any service on ``service_date`` itself.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    stop_coords: dict[str, tuple[float, float]] = {}
    trip_rows: dict[str, list[_StopTimeRow]] = {}
    today_has_service = False

    for spec in feeds:
        try:
            with zipfile.ZipFile(spec.zip_path) as zf:
                feed_stops, feed_rows, feed_today = _load_feed(
                    zf, spec, service_date, days
                )
        except (OSError, zipfile.BadZipFile) as exc:
            logger.warning("GTFS feed %s unusable, skipping: %s", spec.zip_path, exc)
            continue
        stop_coords.update(feed_stops)
        trip_rows.update(feed_rows)
        today_has_service = today_has_service or feed_today

    if not today_has_service:
        raise ValueError(
            f"no GTFS services active on {service_date.isoformat()}; "
            "no feed covers this date"
        )
    patterns, stop_patterns = _build_patterns(trip_rows)
    logger.info(
        "GTFS combined @ %s (+%d days): %d feeds, %d stops, %d patterns, %d trips",
        service_date.isoformat(),
        days - 1,
        len(feeds),
        len(stop_coords),
        len(patterns),
        sum(len(p.trip_departures) for p in patterns),
    )
    return TransitNetwork(
        stop_coords=stop_coords,
        patterns=patterns,
        stop_patterns=stop_patterns,
        service_date=service_date.strftime("%Y%m%d"),
    )


def _load_feed(
    zf: zipfile.ZipFile, spec: FeedSpec, service_date: datetime.date, days: int
) -> tuple[dict[str, tuple[float, float]], dict[str, list[_StopTimeRow]], bool]:
    """One feed → (prefixed stops, prefixed+day-shifted trip rows, has-today)."""
    trips_by_offset = [
        _load_active_trip_ids(
            zf,
            _active_service_ids(zf, service_date + datetime.timedelta(days=offset)),
            spec.route_types,
        )
        for offset in range(days)
    ]
    raw_stops = _load_stops(zf)
    all_trips = set().union(*trips_by_offset)
    raw_rows = _load_stop_times(zf, all_trips, raw_stops)

    rows: dict[str, list[_StopTimeRow]] = {}
    for offset, trips in enumerate(trips_by_offset):
        shift = offset * DAY_S
        for trip_id in trips:
            trip = raw_rows.get(trip_id)
            if not trip:
                continue
            rows[f"{spec.prefix}{offset}:{trip_id}"] = [
                (seq, spec.prefix + stop_id, arr + shift, dep + shift)
                for seq, stop_id, arr, dep in trip
            ]
    prefixed_stops = {spec.prefix + sid: coords for sid, coords in raw_stops.items()}
    return prefixed_stops, rows, bool(trips_by_offset[0])


def parse_gtfs_time(value: str) -> int | None:
    """``HH:MM:SS`` → seconds since service-day midnight; may exceed 24 h."""
    value = value.strip()
    if not value:
        return None
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _dict_rows(zf: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    """Stream a zipped CSV member as dict rows; missing member yields nothing."""
    if member not in zf.namelist():
        logger.warning("GTFS member %s missing, skipping", member)
        return
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _active_service_ids(zf: zipfile.ZipFile, service_date: datetime.date) -> set[str]:
    """calendar.txt weekday rules plus calendar_dates.txt exceptions."""
    date_str = service_date.strftime("%Y%m%d")
    weekday_column = _WEEKDAY_COLUMNS[service_date.weekday()]

    active: set[str] = set()
    for row in _dict_rows(zf, "calendar.txt"):
        in_range = row["start_date"] <= date_str <= row["end_date"]
        if in_range and row[weekday_column] == "1":
            active.add(row["service_id"])

    for row in _dict_rows(zf, "calendar_dates.txt"):
        if row["date"] != date_str:
            continue
        if row["exception_type"] == "1":
            active.add(row["service_id"])
        elif row["exception_type"] == "2":
            active.discard(row["service_id"])
    return active


def _load_stops(zf: zipfile.ZipFile) -> dict[str, tuple[float, float]]:
    """Physical stops only (location_type empty or '0') with valid coords."""
    stops: dict[str, tuple[float, float]] = {}
    skipped = 0
    for row in _dict_rows(zf, "stops.txt"):
        if (row.get("location_type") or "").strip() not in ("", "0"):
            skipped += 1
            continue
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except ValueError:
            skipped += 1
            continue
        stops[row["stop_id"]] = (lat, lon)
    if skipped:
        logger.info("skipped %d non-stop or coordinate-less stops.txt rows", skipped)
    return stops


def _load_active_trip_ids(
    zf: zipfile.ZipFile,
    active_services: set[str],
    route_types: frozenset[int] | set[int] | None = None,
) -> set[str]:
    allowed_routes: set[str] | None = None
    if route_types is not None:
        allowed_routes = set()
        for row in _dict_rows(zf, "routes.txt"):
            try:
                route_type = int(row.get("route_type", ""))
            except ValueError:
                continue
            if route_type in route_types:
                allowed_routes.add(row["route_id"])
    return {
        row["trip_id"]
        for row in _dict_rows(zf, "trips.txt")
        if row["service_id"] in active_services
        and (allowed_routes is None or row["route_id"] in allowed_routes)
    }


def _load_stop_times(
    zf: zipfile.ZipFile,
    active_trips: set[str],
    stop_coords: dict[str, tuple[float, float]],
) -> dict[str, list[_StopTimeRow]]:
    """Stream stop_times.txt; keep rows of active trips at known stops.

    A trip with any row missing both arrival and departure is dropped whole.
    """
    trip_rows: dict[str, list[_StopTimeRow]] = defaultdict(list)
    dropped_trips: set[str] = set()

    with zf.open("stop_times.txt") as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader)
        column = {name: i for i, name in enumerate(header)}
        i_trip = column["trip_id"]
        i_arr = column["arrival_time"]
        i_dep = column["departure_time"]
        i_stop = column["stop_id"]
        i_seq = column["stop_sequence"]

        for row in reader:
            if not row:
                continue
            trip_id = row[i_trip]
            if trip_id not in active_trips:
                continue
            stop_id = row[i_stop]
            if stop_id not in stop_coords:
                continue  # station / unknown stop reference
            arrival = parse_gtfs_time(row[i_arr])
            departure = parse_gtfs_time(row[i_dep])
            if arrival is None:
                arrival = departure
            if departure is None:
                departure = arrival
            if arrival is None or departure is None:
                dropped_trips.add(trip_id)
                continue
            trip_rows[trip_id].append((int(row[i_seq]), stop_id, arrival, departure))

    for trip_id in dropped_trips:
        trip_rows.pop(trip_id, None)
    if dropped_trips:
        logger.info("dropped %d trips with rows missing both times", len(dropped_trips))
    return trip_rows


def _build_patterns(
    trip_rows: dict[str, list[_StopTimeRow]],
) -> tuple[list[Pattern], dict[str, list[tuple[int, int]]]]:
    """Group trips by identical ordered stop sequence into Patterns."""
    groups: dict[tuple[str, ...], list[tuple[list[int], list[int]]]] = defaultdict(list)
    too_short = 0
    for rows in trip_rows.values():
        rows.sort(key=lambda r: r[0])  # stop_sequence is arbitrary but ordered
        if len(rows) < 2:
            too_short += 1
            continue
        key = tuple(r[1] for r in rows)
        departures = [r[3] for r in rows]
        arrivals = [r[2] for r in rows]
        groups[key].append((departures, arrivals))
    if too_short:
        logger.info("dropped %d trips with fewer than 2 usable stops", too_short)

    patterns: list[Pattern] = []
    stop_patterns: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for stop_ids, trips in groups.items():
        trips.sort(key=lambda t: t[0][0])  # by departure at first stop (FIFO)
        p_idx = len(patterns)
        patterns.append(
            Pattern(
                stop_ids=list(stop_ids),
                trip_departures=[t[0] for t in trips],
                trip_arrivals=[t[1] for t in trips],
            )
        )
        for s_idx, stop_id in enumerate(stop_ids):
            stop_patterns[stop_id].append((p_idx, s_idx))
    return patterns, dict(stop_patterns)
