"""Pure OSM tag → routing-attributes model for the street graph.

Shared by ``scripts/build_graph.py`` (preprocessing) and ``server/streets.py``
(runtime speed tables). No I/O, no state — every function maps a plain tag
dict to graph attributes, so the whole access/speed model is unit-testable.

Access and default-speed rules follow the OSRM/Valhalla profiles (simplified).
"""

from __future__ import annotations

# Directionality bitmask stored per edge (undirected storage).
CAR_FWD = 1
CAR_BWD = 2
BIKE_FWD = 4
BIKE_BWD = 8
FOOT = 16  # foot ignores oneway → single bit

# (min_lat, min_lon, max_lat, max_lon)
Bbox = tuple[float, float, float, float]

# Coverage: car keeps everything inside GLOBAL_BBOX
# (SK+AT+CZ+HU+DE+PL+CH+IT-north+SI+HR+RO+RS+UA extracts).
GLOBAL_BBOX: Bbox = (44.5, 5.9, 54.9, 24.5)
# Foot/bike product scope: SK + Vienna/Brno/Budapest fringe. Walking 10 h
# anywhere in the SK region works; walking from Berlin is out of scope.
FOOT_BIKE_BBOX: Bbox = (47.4, 15.9, 49.7, 22.6)

# Directed flag bits per mode: (forward bit, backward bit).
MODE_BITS: dict[str, tuple[int, int]] = {
    "walk": (FOOT, FOOT),
    "bike": (BIKE_FWD, BIKE_BWD),
    "car": (CAR_FWD, CAR_BWD),
}

_FOOT_BIKE_MASK = FOOT | BIKE_FWD | BIKE_BWD


def in_bbox(lat: float, lon: float, bbox: Bbox) -> bool:
    """True when (lat, lon) lies inside the (inclusive) bounding box."""
    min_lat, min_lon, max_lat, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def trim_foot_bike_flags(flags: int, u_inside: bool, v_inside: bool) -> int:
    """Strip foot/bike bits unless BOTH edge endpoints are in FOOT_BIKE_BBOX."""
    if u_inside and v_inside:
        return flags
    return flags & ~_FOOT_BIKE_MASK

# Routable highway classes → compact uint8 enum. Anything not listed here
# (proposed, construction, platform, raceway, bus_guideway, …) is skipped.
CLASS_ID: dict[str, int] = {
    "motorway": 0,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 3,
    "primary": 4,
    "primary_link": 5,
    "secondary": 6,
    "secondary_link": 7,
    "tertiary": 8,
    "tertiary_link": 9,
    "unclassified": 10,
    "residential": 11,
    "living_street": 12,
    "service": 13,
    "track": 14,
    "cycleway": 15,
    "path": 16,
    "footway": 17,
    "pedestrian": 18,
    "steps": 19,
}
NUM_CLASSES = len(CLASS_ID)

# Car: allowed classes with default speed (km/h) when no maxspeed tag exists.
CAR_DEFAULT_KMH: dict[int, int] = {
    CLASS_ID["motorway"]: 90,
    CLASS_ID["motorway_link"]: 45,
    CLASS_ID["trunk"]: 85,
    CLASS_ID["trunk_link"]: 40,
    CLASS_ID["primary"]: 65,
    CLASS_ID["primary_link"]: 30,
    CLASS_ID["secondary"]: 55,
    CLASS_ID["secondary_link"]: 25,
    CLASS_ID["tertiary"]: 40,
    CLASS_ID["tertiary_link"]: 20,
    CLASS_ID["unclassified"]: 25,
    CLASS_ID["residential"]: 25,
    CLASS_ID["living_street"]: 10,
    CLASS_ID["service"]: 15,
}

# Bike: allowed classes with riding speed (km/h). path is conditional
# (bicycle in {yes, designated}) — see classify_way.
BIKE_KMH: dict[int, int] = {
    CLASS_ID["cycleway"]: 15,
    CLASS_ID["primary"]: 15,
    CLASS_ID["primary_link"]: 15,
    CLASS_ID["secondary"]: 15,
    CLASS_ID["secondary_link"]: 15,
    CLASS_ID["tertiary"]: 15,
    CLASS_ID["tertiary_link"]: 15,
    CLASS_ID["unclassified"]: 15,
    CLASS_ID["residential"]: 15,
    CLASS_ID["living_street"]: 15,
    CLASS_ID["service"]: 15,
    CLASS_ID["track"]: 12,
    CLASS_ID["path"]: 13,
    CLASS_ID["footway"]: 4,
    CLASS_ID["pedestrian"]: 4,
    CLASS_ID["steps"]: 2,
}

FOOT_KMH = 5.0

# Foot: allowed classes (trunk/trunk_link only via explicit foot/sidewalk tags).
FOOT_CLASSES: frozenset[int] = frozenset(
    CLASS_ID[name]
    for name in (
        "footway",
        "path",
        "steps",
        "pedestrian",
        "living_street",
        "residential",
        "service",
        "track",
        "unclassified",
        "tertiary",
        "tertiary_link",
        "secondary",
        "secondary_link",
        "primary",
        "primary_link",
        "cycleway",
    )
)

MAXSPEED_NONE_KMH = 130  # "maxspeed=none" (German autobahn style) → 130
MAXSPEED_WALK_KMH = 5
MPH_TO_KMH = 1.609
MAXSPEED_MAX_KMH = 300  # clamp: anything above is tagging garbage

_ONEWAY_YES = frozenset({"yes", "1", "true"})
_ONEWAY_REVERSE = frozenset({"-1", "reverse"})
_ROUNDABOUT = frozenset({"roundabout", "circular"})
_ACCESS_BLOCKED = frozenset({"no", "private"})
_MODE_ALLOWED = frozenset({"yes", "designated"})


def parse_maxspeed(value: str) -> int:
    """Parse an OSM maxspeed tag to km/h; 0 = unknown/unparseable."""
    text = value.strip().lower()
    if not text:
        return 0
    if text == "none":
        return MAXSPEED_NONE_KMH
    if text == "walk":
        return MAXSPEED_WALK_KMH
    if text.endswith("mph"):
        number = text[: -len("mph")].strip()
        if not number.isdigit():
            return 0
        return min(int(round(int(number) * MPH_TO_KMH)), MAXSPEED_MAX_KMH)
    if text.isdigit():
        return min(int(text), MAXSPEED_MAX_KMH)
    return 0


def _oneway_state(tags: dict[str, str]) -> str:
    """'fwd' | 'bwd' | 'both' for cars (roundabout implies oneway=yes)."""
    oneway = tags.get("oneway", "")
    if oneway in _ONEWAY_YES:
        return "fwd"
    if oneway in _ONEWAY_REVERSE:
        return "bwd"
    if oneway == "" and tags.get("junction", "") in _ROUNDABOUT:
        return "fwd"
    return "both"


def _car_flags(tags: dict[str, str], cls: int, oneway: str) -> int:
    if cls not in CAR_DEFAULT_KMH:
        return 0
    motor_vehicle = tags.get("motor_vehicle", "")
    if motor_vehicle == "no":
        return 0
    if tags.get("access", "") in _ACCESS_BLOCKED and motor_vehicle != "yes":
        return 0
    if oneway == "fwd":
        return CAR_FWD
    if oneway == "bwd":
        return CAR_BWD
    return CAR_FWD | CAR_BWD


def _bike_oneway(tags: dict[str, str], car_oneway: str) -> str:
    """Bike directionality: oneway applies, with cycling-specific overrides."""
    oneway_bicycle = tags.get("oneway:bicycle", "")
    if oneway_bicycle == "no":
        return "both"
    if oneway_bicycle in _ONEWAY_REVERSE:
        return "bwd"
    if oneway_bicycle in _ONEWAY_YES:
        return "fwd"
    if tags.get("cycleway", "").startswith("opposite"):
        return "both"
    if tags.get("cycleway:left", "").startswith("opposite"):
        return "both"
    return car_oneway


def _bike_flags(tags: dict[str, str], cls: int, car_oneway: str) -> int:
    if cls not in BIKE_KMH:
        return 0
    bicycle = tags.get("bicycle", "")
    if cls == CLASS_ID["path"] and bicycle not in _MODE_ALLOWED:
        return 0
    if bicycle in ("no", "use_sidepath"):
        return 0
    if tags.get("motorroad", "") == "yes":
        return 0
    if tags.get("access", "") in _ACCESS_BLOCKED and bicycle not in _MODE_ALLOWED:
        return 0
    oneway = _bike_oneway(tags, car_oneway)
    if oneway == "fwd":
        return BIKE_FWD
    if oneway == "bwd":
        return BIKE_BWD
    return BIKE_FWD | BIKE_BWD


def _sidewalk_present(tags: dict[str, str]) -> bool:
    return tags.get("sidewalk", "") in ("yes", "both", "left", "right")


def _foot_flags(tags: dict[str, str], cls: int) -> int:
    foot = tags.get("foot", "")
    allowed = cls in FOOT_CLASSES
    if cls in (CLASS_ID["trunk"], CLASS_ID["trunk_link"]):
        allowed = foot in _MODE_ALLOWED or _sidewalk_present(tags)
    if not allowed:
        return 0
    if foot == "no":
        return 0
    if tags.get("motorroad", "") == "yes":
        return 0
    if tags.get("access", "") in _ACCESS_BLOCKED and foot not in _MODE_ALLOWED:
        return 0
    return FOOT


def classify_way(tags: dict[str, str]) -> tuple[int, int, int] | None:
    """Map a way's tags to (flags, highway_class, maxspeed_kmh).

    Returns None when the way is not routable by any mode. maxspeed_kmh is
    the parsed tag value (0 = untagged); per-mode speeds are applied at
    graph-load time, never baked into the stored file.
    """
    highway = tags.get("highway")
    if highway is None:
        return None
    cls = CLASS_ID.get(highway)
    if cls is None:
        return None
    oneway = _oneway_state(tags)
    flags = (
        _car_flags(tags, cls, oneway)
        | _bike_flags(tags, cls, oneway)
        | _foot_flags(tags, cls)
    )
    if flags == 0:
        return None
    return flags, cls, parse_maxspeed(tags.get("maxspeed", ""))
