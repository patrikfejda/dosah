"""Tests for the pure OSM tag → routing-attributes model (streets_model)."""

from __future__ import annotations

from server.streets_model import (
    BIKE_BWD,
    BIKE_FWD,
    BIKE_KMH,
    CAR_BWD,
    CAR_DEFAULT_KMH,
    CAR_FWD,
    CLASS_ID,
    FOOT,
    FOOT_BIKE_BBOX,
    GLOBAL_BBOX,
    classify_way,
    in_bbox,
    parse_maxspeed,
    trim_foot_bike_flags,
)

CAR_BOTH = CAR_FWD | CAR_BWD
BIKE_BOTH = BIKE_FWD | BIKE_BWD


def _flags(tags: dict[str, str]) -> int:
    result = classify_way(tags)
    assert result is not None, f"expected routable way for {tags}"
    return result[0]


def test_motorway_is_car_only_with_default_90() -> None:
    result = classify_way({"highway": "motorway"})
    assert result is not None
    flags, cls, maxspeed = result
    assert flags == CAR_BOTH
    assert cls == CLASS_ID["motorway"]
    assert maxspeed == 0  # no maxspeed tag → 0 stored, default applied at runtime
    assert CAR_DEFAULT_KMH[CLASS_ID["motorway"]] == 90


def test_residential_allows_all_modes() -> None:
    flags = _flags({"highway": "residential"})
    assert flags == CAR_BOTH | BIKE_BOTH | FOOT


def test_oneway_yes_restricts_forward_only() -> None:
    flags = _flags({"highway": "primary", "oneway": "yes"})
    assert flags & (CAR_FWD | CAR_BWD) == CAR_FWD
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_FWD
    assert flags & FOOT  # foot ignores oneway


def test_oneway_minus_one_reverses() -> None:
    flags = _flags({"highway": "primary", "oneway": "-1"})
    assert flags & (CAR_FWD | CAR_BWD) == CAR_BWD
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_BWD


def test_roundabout_implies_oneway() -> None:
    flags = _flags({"highway": "primary", "junction": "roundabout"})
    assert flags & (CAR_FWD | CAR_BWD) == CAR_FWD
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_FWD


def test_oneway_bicycle_no_restores_both_bike_directions() -> None:
    flags = _flags(
        {"highway": "residential", "oneway": "yes", "oneway:bicycle": "no"}
    )
    assert flags & (CAR_FWD | CAR_BWD) == CAR_FWD
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_BOTH


def test_oneway_bicycle_minus_one_reverses_bike() -> None:
    flags = _flags({"highway": "residential", "oneway:bicycle": "-1"})
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_BWD


def test_cycleway_opposite_lane_restores_both_bike_directions() -> None:
    flags = _flags(
        {"highway": "residential", "oneway": "yes", "cycleway": "opposite_lane"}
    )
    assert flags & (BIKE_FWD | BIKE_BWD) == BIKE_BOTH


def test_maxspeed_parsing() -> None:
    assert parse_maxspeed("50") == 50
    assert parse_maxspeed("30 mph") == 48  # 30 * 1.609 = 48.27 → 48
    assert parse_maxspeed("none") == 130
    assert parse_maxspeed("walk") == 5
    assert parse_maxspeed("garbage") == 0
    assert parse_maxspeed("") == 0
    result = classify_way({"highway": "secondary", "maxspeed": "70"})
    assert result is not None and result[2] == 70


def test_access_private_excludes_everything() -> None:
    assert classify_way({"highway": "residential", "access": "private"}) is None


def test_access_private_with_motor_vehicle_yes_allows_car() -> None:
    flags = _flags(
        {"highway": "service", "access": "private", "motor_vehicle": "yes"}
    )
    assert flags & CAR_BOTH == CAR_BOTH
    assert flags & (BIKE_FWD | BIKE_BWD | FOOT) == 0


def test_motor_vehicle_no_excludes_car_only() -> None:
    flags = _flags({"highway": "residential", "motor_vehicle": "no"})
    assert flags & CAR_BOTH == 0
    assert flags & FOOT
    assert flags & BIKE_BOTH == BIKE_BOTH


def test_footway_is_foot_and_slow_bike() -> None:
    flags = _flags({"highway": "footway"})
    assert flags & FOOT
    assert flags & BIKE_BOTH == BIKE_BOTH
    assert flags & CAR_BOTH == 0
    assert BIKE_KMH[CLASS_ID["footway"]] == 4


def test_steps_no_car_slow_bike_foot_ok() -> None:
    flags = _flags({"highway": "steps"})
    assert flags & CAR_BOTH == 0
    assert flags & FOOT
    assert flags & BIKE_BOTH == BIKE_BOTH
    assert BIKE_KMH[CLASS_ID["steps"]] == 2


def test_path_without_bicycle_tag_is_foot_only() -> None:
    flags = _flags({"highway": "path"})
    assert flags == FOOT


def test_path_with_bicycle_designated_adds_bike_13() -> None:
    flags = _flags({"highway": "path", "bicycle": "designated"})
    assert flags & BIKE_BOTH == BIKE_BOTH
    assert BIKE_KMH[CLASS_ID["path"]] == 13


def test_trunk_car_only_unless_foot_yes() -> None:
    assert _flags({"highway": "trunk"}) == CAR_BOTH
    flags = _flags({"highway": "trunk", "foot": "yes"})
    assert flags & FOOT


def test_bicycle_use_sidepath_excludes_bike() -> None:
    flags = _flags({"highway": "secondary", "bicycle": "use_sidepath"})
    assert flags & BIKE_BOTH == 0
    assert flags & CAR_BOTH == CAR_BOTH
    assert flags & FOOT


def test_motorroad_excludes_bike_and_foot() -> None:
    flags = _flags({"highway": "primary", "motorroad": "yes"})
    assert flags & (BIKE_BOTH | FOOT) == 0
    assert flags & CAR_BOTH == CAR_BOTH


def test_cycleway_foot_no() -> None:
    flags = _flags({"highway": "cycleway"})
    assert flags & FOOT
    assert flags & BIKE_BOTH == BIKE_BOTH
    flags = _flags({"highway": "cycleway", "foot": "no"})
    assert flags & FOOT == 0


def test_in_bbox_boundaries() -> None:
    assert in_bbox(48.15, 17.11, FOOT_BIKE_BBOX)  # Bratislava
    assert in_bbox(48.21, 16.37, FOOT_BIKE_BBOX)  # Vienna
    assert not in_bbox(52.52, 13.40, FOOT_BIKE_BBOX)  # Berlin
    assert in_bbox(52.52, 13.40, GLOBAL_BBOX)  # Berlin is car scope
    assert not in_bbox(59.33, 18.07, GLOBAL_BBOX)  # Stockholm is out entirely
    # Inclusive edges.
    assert in_bbox(FOOT_BIKE_BBOX[0], FOOT_BIKE_BBOX[1], FOOT_BIKE_BBOX)
    assert in_bbox(FOOT_BIKE_BBOX[2], FOOT_BIKE_BBOX[3], FOOT_BIKE_BBOX)


def test_trim_foot_bike_flags_keeps_all_inside() -> None:
    flags = CAR_BOTH | BIKE_BOTH | FOOT
    assert trim_foot_bike_flags(flags, True, True) == flags


def test_trim_foot_bike_flags_strips_when_any_endpoint_outside() -> None:
    flags = CAR_BOTH | BIKE_BOTH | FOOT
    assert trim_foot_bike_flags(flags, True, False) == CAR_BOTH
    assert trim_foot_bike_flags(flags, False, True) == CAR_BOTH
    assert trim_foot_bike_flags(flags, False, False) == CAR_BOTH
    # Foot/bike-only edge outside the bbox loses everything.
    assert trim_foot_bike_flags(BIKE_BOTH | FOOT, False, False) == 0


def test_unroutable_highways_are_skipped() -> None:
    for value in ("proposed", "construction", "platform", "raceway", "elevator"):
        assert classify_way({"highway": value}) is None
    assert classify_way({"building": "yes"}) is None
