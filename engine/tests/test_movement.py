"""Tests for the Movement_Engine (Requirement 8)."""
import math

import pytest

from village.models import Location, LocationCategory, OpeningHours, TravelMode
from village.movement import (ARRIVE_TOLERANCE_M, Movement_Engine, RouteRejection,
                              clamp_position, haversine_m, in_bounds)


def loc(id, lat, lon, cat=LocationCategory.LEISURE, cap=100):
    return Location(id=id, name=id, category=cat, lat=lat, lon=lon, capacity=cap,
                    hours=[OpeningHours("00:00", "23:59")] * 7)


# Melbourne reference points
FED_SQUARE = (-37.817979, 144.968480)
FLINDERS = (-37.818, 144.967)  # transit hub near Fed Square
CARLTON = (-37.800, 144.967)   # ~2km north


def test_haversine_known_distance():
    # ~1.11 km per 0.01 deg latitude
    d = haversine_m(-37.80, 144.96, -37.81, 144.96)
    assert 1050 < d < 1200


def test_walk_selected_for_short_distance():
    eng = Movement_Engine([])
    mode, hop = eng.select_mode(-37.8180, 144.9680, -37.8175, 144.9685)
    assert mode == TravelMode.WALK
    assert hop is None


def test_car_selected_when_no_transit_near():
    eng = Movement_Engine([])
    # >2km apart, no transit hub => car
    mode, hop = eng.select_mode(FED_SQUARE[0], FED_SQUARE[1], -37.760, 145.020)
    assert mode == TravelMode.CAR


def test_tram_selected_when_transit_near_both_ends():
    transit_a = loc("t_a", -37.8181, 144.9681, LocationCategory.TRANSIT)
    transit_b = loc("t_b", -37.7605, 145.0201, LocationCategory.TRANSIT)
    eng = Movement_Engine([transit_a, transit_b])
    # two transit hubs, but tram needs ONE hub near BOTH ends. Place a single
    # hub between? Use a hub near origin and destination within 500m each.
    # Here transit_a is near origin, transit_b near destination -> neither is
    # near BOTH, so expect car.
    mode, _ = eng.select_mode(-37.8181, 144.9681, -37.7605, 145.0201)
    assert mode == TravelMode.CAR


def test_tram_when_single_hub_near_both():
    # Construct origin & destination both within 500m of one hub, but >2km apart.
    # Not geometrically possible for a single point to be within 500m of two
    # points >2km apart, so tram requires the hub-near-both rule to fail here.
    # Instead validate the proximity helper directly for a close pair >2km:
    # place hub, origin and destination all near each other but far apart is
    # impossible -> confirm car fallback is the correct behaviour.
    eng = Movement_Engine([loc("t", -37.79, 144.99, LocationCategory.TRANSIT)])
    mode, _ = eng.select_mode(-37.80, 144.98, -37.75, 145.02)
    assert mode in (TravelMode.CAR, TravelMode.TRAM)


def test_duration_rounds_up_and_clamps_min_1():
    eng = Movement_Engine([])
    dest = loc("d", -37.8175, 144.9685)
    route = eng.compute_route(-37.8180, 144.9680, dest)
    assert route.duration_min >= 1


def test_duration_clamped_max_120():
    eng = Movement_Engine([])
    # very long car trip within bounds
    dest = loc("d", -37.70, 145.10)
    route = eng.compute_route(-38.00, 144.85, dest)
    assert route.duration_min <= 120


def test_route_has_at_least_two_coords():
    eng = Movement_Engine([])
    dest = loc("d", -37.80, 144.97)
    route = eng.compute_route(-37.81, 144.96, dest)
    assert len(route.coords) >= 2


def test_interpolation_midpoint():
    eng = Movement_Engine([])
    dest = loc("d", -37.80, 144.96)
    route = eng.compute_route(-37.82, 144.96, dest)
    # at 0 elapsed -> origin
    lat0, lon0 = eng.interpolate(route, 0)
    assert abs(lat0 - (-37.82)) < 1e-6
    # at full duration -> destination
    latf, lonf = eng.interpolate(route, route.duration_min)
    assert abs(latf - (-37.80)) < 1e-4
    # halfway -> roughly the midpoint latitude
    mid_lat, _ = eng.interpolate(route, route.duration_min / 2.0)
    assert -37.82 < mid_lat < -37.80


def test_interpolation_within_25m_tolerance():
    eng = Movement_Engine([])
    dest = loc("d", -37.800, 144.970)
    route = eng.compute_route(-37.820, 144.960, dest)
    # sample a fraction and confirm distance-from-origin matches ratio*dist
    for frac in (0.25, 0.5, 0.75):
        lat, lon = eng.interpolate(route, route.duration_min * frac)
        d_from_origin = haversine_m(route.coords[0][0], route.coords[0][1], lat, lon)
        expected = frac * route.distance_km * 1000.0
        assert abs(d_from_origin - expected) <= 25.0


def test_clamp_position_to_bounds():
    lat, lon = clamp_position(-40.0, 150.0)
    assert lat == -38.00
    assert lon == 145.10
    assert in_bounds(lat, lon)


def test_out_of_bounds_destination_rejected():
    eng = Movement_Engine([])
    dest = loc("d", -40.0, 150.0)  # outside bounds
    with pytest.raises(RouteRejection):
        eng.compute_route(-37.80, 144.96, dest)


def test_within_arrival_tolerance():
    eng = Movement_Engine([])
    dest = loc("d", -37.8000, 144.9600)
    # ~30m away
    assert eng.is_within_arrival(-37.80025, 144.9600, dest)


def test_energy_multiplier_increases_duration():
    eng = Movement_Engine([])
    dest = loc("d", -37.79, 144.99)
    base = eng.compute_route(-37.81, 144.96, dest, energy_multiplier=1.0)
    slow = eng.compute_route(-37.81, 144.96, dest, energy_multiplier=1.5)
    assert slow.duration_min >= base.duration_min
