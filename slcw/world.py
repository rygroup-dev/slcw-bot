"""World map, distances, and travel planning.

Coordinates and the travel formula are taken from the frontend:

    travelTimeSeconds = round(20 * distance * (1 - speedReduction))

with distance the Euclidean gap between two locations on a 0-100 grid. Mounts
supply the speed reduction; without one it is zero.

Travel is what makes the production chain self-running. Gathering happens at farm
zones and refining at city workshops, so a wallet that cannot move is stuck with
whatever its current location happens to offer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_DISTANCE = 20


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    type: str
    x: float
    y: float

    @property
    def is_city(self) -> bool:
        return self.type == "city"

    @property
    def is_farm(self) -> bool:
        return self.type == "farm_zone"


def _loc(id_, name, type_, x, y):
    return Location(id=id_, name=name, type=type_, x=x, y=y)


LOCATIONS = {loc.id: loc for loc in (
    _loc("city_1", "Agnos", "city", 15.5, 35),
    _loc("city_2", "Virtan", "city", 66, 18),
    _loc("city_3", "Mialor", "city", 7.5, 58),
    _loc("city_4", "Faris", "city", 45, 64),
    _loc("city_5", "Seorn", "city", 52, 36),
    _loc("city_7", "Elar", "city", 60, 75),
    _loc("city_11", "Norsal", "city", 20, 81),
    _loc("city_13", "Fardelin", "city", 32, 58),
    _loc("city_14", "Ostrim", "city", 62, 66),
    _loc("city_15", "Lismar", "city", 38, 19),
    _loc("city_17", "Greyholm", "city", 62, 55),
    _loc("city_18", "Evora", "city", 53, 88),
    _loc("farm_1", "Whispering Woods", "farm_zone", 38, 35),
    _loc("farm_2", "Crystal Cave", "farm_zone", 10, 20),
    _loc("farm_3", "Borderlands", "farm_zone", 72, 42),
    _loc("farm_4", "Fiber Plantations", "farm_zone", 43, 80),
    _loc("farm_5", "Hunters Path", "farm_zone", 34, 51),
)}


def distance(from_id: str, to_id: str) -> float:
    origin, target = LOCATIONS.get(from_id), LOCATIONS.get(to_id)
    if origin is None or target is None:
        return float("inf")
    return math.hypot(target.x - origin.x, target.y - origin.y)


def travel_seconds(from_id: str, to_id: str, speed_reduction: float = 0.0) -> float:
    """Seconds to travel, matching the client's own estimate."""
    gap = distance(from_id, to_id)
    if gap == float("inf"):
        return float("inf")
    # The client clamps the mount bonus at 75%.
    reduction = max(0.0, min(0.75, speed_reduction))
    return round(SECONDS_PER_DISTANCE * gap * (1 - reduction))


def name_of(location_id: str) -> str:
    location = LOCATIONS.get(location_id)
    return location.name if location else location_id


def nearest(from_id: str, candidates) -> str | None:
    options = [c for c in candidates if c in LOCATIONS and c != from_id]
    return min(options, key=lambda c: distance(from_id, c)) if options else None


def economic_locations() -> list[str]:
    """Locations the engine has a reason to visit.

    Every gathering site, every refining city, and the production city. Travelling
    anywhere else costs time for no modelled return.
    """
    from . import farming, refining

    places = set(farming.FARM_LOCATIONS)
    places |= {w.city_id for w in refining.WORKSHOPS.values()}
    places.add("city_2")  # production
    places.add("farm_3")  # battles
    return sorted(places)
