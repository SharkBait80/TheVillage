"""Tests for Simulation_Clock (Requirement 1)."""
from datetime import datetime

import pytest

from village.clock import (ACCEL_DEFAULT, InvalidAccelerationError,
                           Simulation_Clock, localize)


class FakeClock:
    """Manually-driven monotonic real clock."""
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def start_time():
    # 2026-03-02 is a Monday; Melbourne is +11:00 (AEDT) in March.
    return localize(datetime.fromisoformat("2026-03-02T06:00:00"))


def test_default_accel_is_four():
    clk = Simulation_Clock(start_time(), real_clock=FakeClock())
    assert clk.acceleration_factor == ACCEL_DEFAULT == 4


def test_tick_boundaries_one_per_minute():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    # 4x accel: 15 real seconds => 60 sim seconds => exactly one minute boundary
    fake.tick(15.0)
    res = clk.advance()
    assert len(res.ticks) == 1
    # advance another 45 real sec => 180 sim sec => 3 more boundaries
    fake.tick(45.0)
    res = clk.advance()
    assert len(res.ticks) == 3
    # boundaries strictly ascending
    times = [t.sim_time for t in res.ticks]
    assert times == sorted(times)


def test_no_boundary_emitted_twice_and_none_omitted():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=60, real_clock=fake)
    # 60x: 1 real second = 60 sim seconds = 1 minute
    emitted = []
    for _ in range(10):
        fake.tick(1.0)
        emitted.extend(t.sim_time for t in clk.advance().ticks)
    assert len(emitted) == 10
    assert len(set(emitted)) == 10  # unique
    # contiguous minute sequence
    for i in range(1, len(emitted)):
        assert (emitted[i] - emitted[i - 1]).total_seconds() == 60


def test_accel_change_continues_from_current_sim_time():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    fake.tick(15.0)
    clk.advance()  # +1 minute
    t_before = clk.sim_time
    clk.set_acceleration(60)
    assert clk.acceleration_factor == 60
    # sim time unchanged by the accel switch itself
    assert clk.sim_time == t_before
    fake.tick(1.0)  # at 60x => +60 sim seconds => 1 minute
    res = clk.advance()
    assert len(res.ticks) == 1


def test_invalid_accel_rejected_and_retained():
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=FakeClock())
    for bad in (0, 61, -5, 3.5, True):
        with pytest.raises(InvalidAccelerationError):
            clk.set_acceleration(bad)
    assert clk.acceleration_factor == 4


def test_pause_holds_sim_time_no_ticks():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    fake.tick(15.0)
    clk.advance()
    held = clk.sim_time
    clk.pause()
    fake.tick(100.0)  # real time passes while paused
    res = clk.advance()
    assert res.ticks == []
    assert clk.sim_time == held


def test_resume_excludes_paused_interval_from_drift():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    fake.tick(60.0)
    clk.advance()
    clk.pause()
    fake.tick(3600.0)  # 1h paused
    clk.resume()
    fake.tick(60.0)
    clk.advance()
    # drift stays well within 60s/day tolerance despite the long pause
    assert abs(clk.drift_seconds()) < 60.0


def test_drift_within_tolerance_over_a_day():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    # simulate one sim day: 1440 minutes at 4x => 21600 real seconds
    # advance in 15s real steps (=1 sim min each)
    for _ in range(1440):
        fake.tick(15.0)
        clk.advance()
    # exposed sim time ~= 4 * total real; drift < 60s per sim day (Req 1.5)
    assert abs(clk.drift_seconds()) < 60.0


def test_day_rollover_emitted_once_after_midnight_tick():
    fake = FakeClock()
    # start at 23:58 so we cross midnight quickly
    start = localize(datetime.fromisoformat("2026-03-02T23:58:00"))
    clk = Simulation_Clock(start, acceleration_factor=60, real_clock=fake)
    rollovers = []
    ticks = []
    for _ in range(5):  # 5 minutes
        fake.tick(1.0)
        r = clk.advance()
        ticks.extend(r.ticks)
        rollovers.extend(r.day_rollovers)
    # exactly one rollover for the completed date 2026-03-02
    assert len(rollovers) == 1
    assert rollovers[0].completed_date == "2026-03-02"
    # midnight tick present
    assert any(t.sim_time.hour == 0 and t.sim_time.minute == 0 for t in ticks)


def test_iso_has_melbourne_offset():
    clk = Simulation_Clock(start_time(), real_clock=FakeClock())
    iso = clk.sim_time_iso()
    # March => AEDT +11:00
    assert iso.endswith("+11:00")


def test_lag_warning_when_processing_exceeds_budget():
    fake = FakeClock()
    clk = Simulation_Clock(start_time(), acceleration_factor=4, real_clock=fake)
    fake.tick(15.0)
    # budget per tick = 60/4 = 15 real sec; report 20s processing => lag
    res = clk.advance(tick_processing_seconds=20.0)
    assert res.lag_warning is not None
    assert res.lag_warning.backlog_ticks == len(res.ticks)
