"""Movement_Engine — routes, travel modes, duration, interpolation (Requirement 8).

Straight-line (haversine) routing with an optional transit hop. Deterministic
and pure: no I/O. Positions are clamped to the Melbourne map bounds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import MAP_LAT_MIN, MAP_LAT_MAX, MAP_LON_MIN, MAP_LON_MAX
from .models import Location, LocationCategory, TravelMode

EARTH_RADIUS_M = 6_371_000.0

SPEED_KMH = {TravelMode.WALK: 5.0, TravelMode.TRAM: 20.0, TravelMode.CAR: 30.0}

TRAM_DIST_THRESHOLD_KM = 2.0    # Req 8.5 / 8.8
TRANSIT_PROXIMITY_M = 500.0     # Req 8.5
ARRIVE_TOLERANCE_M = 50.0       # within-50m completes next tick (Req 8.10)
INTERP_TOLERANCE_M = 25.0       # position interpolation tolerance (Req 8.3)
DURATION_MIN = 1
DURATION_MAX = 120


class RouteRejection(Exception):
    def __init__(self, reason: str, next_opening: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.next_opening = next_opening


@dataclass
class Route:
    coords: List[List[float]]        # >=2 [lat, lon] points
    mode: TravelMode
    distance_km: float
    duration_min: int


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def clamp_position(lat: float, lon: float) -> Tuple[float, float]:
    """Clamp a position to the map bounds (Req 8.6)."""
    return (
        max(MAP_LAT_MIN, min(MAP_LAT_MAX, lat)),
        max(MAP_LON_MIN, min(MAP_LON_MAX, lon)),
    )


def in_bounds(lat: float, lon: float) -> bool:
    return MAP_LAT_MIN <= lat <= MAP_LAT_MAX and MAP_LON_MIN <= lon <= MAP_LON_MAX


class Movement_Engine:
    """Computes routes and interpolated positions for travelling agents."""

    def __init__(self, transit_locations: Optional[List[Location]] = None):
        # transit hubs used for tram-mode selection.
        self.transit_locations = [
            loc for loc in (transit_locations or [])
            if loc.category == LocationCategory.TRANSIT
        ]

    # -- mode selection -----------------------------------------------------
    def select_mode(self, o_lat: float, o_lon: float,
                    d_lat: float, d_lon: float) -> Tuple[TravelMode, Optional[Location]]:
        """Select travel mode per Req 8.5 / 8.8.

        tram if straight dist > 2km AND a transit loc within 500m of BOTH ends;
        else walk for <=2km, car for >2km. Returns (mode, transit_hop_or_None).
        """
        dist_km = haversine_m(o_lat, o_lon, d_lat, d_lon) / 1000.0
        if dist_km > TRAM_DIST_THRESHOLD_KM:
            hop = self._transit_near_both(o_lat, o_lon, d_lat, d_lon)
            if hop is not None:
                return TravelMode.TRAM, hop
            return TravelMode.CAR, None
        return TravelMode.WALK, None

    def _transit_near_both(self, o_lat, o_lon, d_lat, d_lon) -> Optional[Location]:
        for loc in self.transit_locations:
            near_o = haversine_m(o_lat, o_lon, loc.lat, loc.lon) <= TRANSIT_PROXIMITY_M
            near_d = haversine_m(d_lat, d_lon, loc.lat, loc.lon) <= TRANSIT_PROXIMITY_M
            if near_o and near_d:
                return loc
        return None

    # -- routing ------------------------------------------------------------
    def compute_route(self, o_lat: float, o_lon: float,
                      dest: Location, energy_multiplier: float = 1.0) -> Route:
        """Compute a route to a destination Location (Req 8.1/8.2).

        Raises RouteRejection if the destination is out of bounds (Req 8.9).
        `energy_multiplier` (1.5 when energy critical, Req 5.7) is applied to
        the duration.
        """
        if not in_bounds(dest.lat, dest.lon):
            raise RouteRejection("out_of_bounds")

        mode, hop = self.select_mode(o_lat, o_lon, dest.lat, dest.lon)

        # Build coordinate list: origin -> [transit hop] -> destination.
        coords: List[List[float]] = [[o_lat, o_lon]]
        if hop is not None:
            coords.append([hop.lat, hop.lon])
        coords.append([dest.lat, dest.lon])

        # Route distance = sum of straight-line segments.
        dist_km = _route_distance_km(coords)

        speed = SPEED_KMH[mode]
        raw_min = math.ceil(dist_km / speed * 60.0)
        raw_min = int(math.ceil(raw_min * energy_multiplier)) if energy_multiplier != 1.0 else raw_min
        duration = max(DURATION_MIN, min(DURATION_MAX, raw_min))

        return Route(coords=coords, mode=mode, distance_km=dist_km, duration_min=duration)

    def is_within_arrival(self, lat: float, lon: float, dest: Location) -> bool:
        """True if position is within 50m of destination (Req 8.10)."""
        return haversine_m(lat, lon, dest.lat, dest.lon) <= ARRIVE_TOLERANCE_M

    # -- interpolation ------------------------------------------------------
    def interpolate(self, route: Route, elapsed_min: float) -> Tuple[float, float]:
        """Position along the route at elapsed/expected ratio (Req 8.3).

        Clamped to map bounds (Req 8.6).
        """
        if route.duration_min <= 0:
            lat, lon = route.coords[-1]
            return clamp_position(lat, lon)
        ratio = max(0.0, min(1.0, elapsed_min / route.duration_min))
        target_dist_km = ratio * route.distance_km
        lat, lon = _point_at_distance(route.coords, target_dist_km)
        return clamp_position(lat, lon)


def _route_distance_km(coords: List[List[float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_m(coords[i][0], coords[i][1],
                             coords[i + 1][0], coords[i + 1][1])
    return total / 1000.0


def _point_at_distance(coords: List[List[float]], dist_km: float) -> Tuple[float, float]:
    """Linearly interpolate a point `dist_km` along the polyline."""
    if dist_km <= 0:
        return coords[0][0], coords[0][1]
    remaining_m = dist_km * 1000.0
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        seg_m = haversine_m(a[0], a[1], b[0], b[1])
        if seg_m == 0:
            continue
        if remaining_m <= seg_m:
            t = remaining_m / seg_m
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        remaining_m -= seg_m
    return coords[-1][0], coords[-1][1]


__all__ = [
    "Movement_Engine", "Route", "RouteRejection",
    "haversine_m", "clamp_position", "in_bounds",
    "SPEED_KMH", "TRAM_DIST_THRESHOLD_KM", "TRANSIT_PROXIMITY_M",
    "ARRIVE_TOLERANCE_M", "INTERP_TOLERANCE_M", "DURATION_MIN", "DURATION_MAX",
]
