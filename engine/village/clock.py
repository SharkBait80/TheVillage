"""Simulation_Clock — accelerated sim clock (Requirement 1).

Advances Simulated_Time by acceleration_factor * elapsed real seconds and emits
one Tick per crossed sim-minute boundary, day-rollover events on midnight
crossings, and lag warnings when a tick's real-time budget is exceeded.

Determinism: the real clock is *injected* as a monotonic-seconds callable so
tests can drive time explicitly. Timezone handling uses zoneinfo for
DST-correct offsets on the simulated date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

TIMEZONE = "Australia/Melbourne"

ACCEL_MIN = 1
ACCEL_MAX = 60
ACCEL_DEFAULT = 4


class InvalidAccelerationError(ValueError):
    """Raised/returned when an acceleration factor is out of range."""


@dataclass
class Tick:
    """One simulated-minute boundary crossing."""
    sim_time: datetime  # tz-aware, whole-minute boundary


@dataclass
class DayRollover:
    completed_date: str  # ISO date (YYYY-MM-DD) of the completed simulated day


@dataclass
class LagWarning:
    backlog_ticks: int


@dataclass
class AdvanceResult:
    ticks: List[Tick]
    day_rollovers: List[DayRollover]
    lag_warning: Optional[LagWarning]


def _melb_tz() -> ZoneInfo:
    return ZoneInfo(TIMEZONE)


def localize(dt: datetime) -> datetime:
    """Ensure a datetime carries the Melbourne offset in effect on its date."""
    tz = _melb_tz()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


class Simulation_Clock:
    """Accelerated clock. Real time supplied by a monotonic seconds callable.

    Usage:
        clk = Simulation_Clock(start_sim_time, real_clock=time.monotonic)
        result = clk.advance()  # call each real loop iteration
    """

    def __init__(
        self,
        start_sim_time: datetime,
        acceleration_factor: int = ACCEL_DEFAULT,
        real_clock: Optional[Callable[[], float]] = None,
    ):
        if not _valid_accel(acceleration_factor):
            raise InvalidAccelerationError(
                f"acceleration_factor must be an integer {ACCEL_MIN}..{ACCEL_MAX}"
            )
        if real_clock is None:
            import time as _time
            real_clock = _time.monotonic
        self._real_clock = real_clock

        self._sim_time = localize(start_sim_time)
        # last emitted whole-minute boundary (floor of sim_time to the minute)
        self._last_boundary = self._sim_time.replace(second=0, microsecond=0)
        self._accel = int(acceleration_factor)

        self._real_anchor = real_clock()  # real seconds at last advance
        self._paused = False
        self._pause_started_real: Optional[float] = None
        self._paused_real_total = 0.0  # excluded real seconds (Req 9)

        # drift tracking (Req 5): running-real-seconds since start, and the
        # sim seconds we *should* have advanced given accel changes.
        self._expected_sim_seconds = 0.0
        self._running_real_seconds = 0.0
        self._start_sim_time = self._sim_time

        # lag warning throttle (Req 7): at most once / 10 real seconds.
        self._last_lag_warn_real: Optional[float] = None
        self._tick_real_cost = 0.0  # accumulated tick processing cost hint

    # -- properties ---------------------------------------------------------
    @property
    def sim_time(self) -> datetime:
        return self._sim_time

    @property
    def acceleration_factor(self) -> int:
        return self._accel

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def paused_real_total(self) -> float:
        return self._paused_real_total

    def sim_time_iso(self) -> str:
        """ISO-8601, whole-second resolution, explicit Melbourne offset (Req 1.1)."""
        return self._sim_time.replace(microsecond=0).isoformat()

    # -- controls -----------------------------------------------------------
    def set_acceleration(self, factor: int) -> None:
        """Change accel (Req 1.3/1.10). Rejects invalid values without disruption."""
        if not _valid_accel(factor):
            raise InvalidAccelerationError(
                f"acceleration_factor must be an integer {ACCEL_MIN}..{ACCEL_MAX}; "
                f"retained {self._accel}"
            )
        # Settle expected-sim accounting at the *old* rate up to now, then switch.
        self._accrue(self._now())
        self._accel = int(factor)

    def pause(self) -> None:
        if self._paused:
            return
        self._accrue(self._now())
        self._paused = True
        self._pause_started_real = self._now()

    def resume(self) -> None:
        if not self._paused:
            return
        now = self._now()
        if self._pause_started_real is not None:
            self._paused_real_total += now - self._pause_started_real
        self._paused = False
        self._pause_started_real = None
        self._real_anchor = now  # exclude paused interval (Req 9)

    # -- core advance -------------------------------------------------------
    def advance(self, tick_processing_seconds: float = 0.0) -> AdvanceResult:
        """Advance sim time based on real elapsed since last call.

        `tick_processing_seconds` optionally reports how long the *previous*
        tick batch took, used for the lag-warning budget check (Req 1.7).
        Returns ticks (one per crossed minute), day rollovers, and any lag
        warning.
        """
        now = self._now()
        if self._paused:
            # Held: no advance, no ticks (Req 1.8).
            self._real_anchor = now
            return AdvanceResult([], [], None)

        elapsed_real = now - self._real_anchor
        self._real_anchor = now
        if elapsed_real < 0:
            elapsed_real = 0.0

        # Advance sim time by accel * elapsed real seconds (Req 1.2/1.3).
        sim_advance = timedelta(seconds=self._accel * elapsed_real)
        self._sim_time = self._sim_time + sim_advance

        # Drift accounting (Req 5).
        self._running_real_seconds += elapsed_real
        self._expected_sim_seconds += self._accel * elapsed_real

        # Emit one Tick per crossed minute boundary (Req 1.4), ascending.
        ticks: List[Tick] = []
        rollovers: List[DayRollover] = []
        boundary = self._last_boundary
        while boundary + timedelta(minutes=1) <= self._sim_time:
            boundary = boundary + timedelta(minutes=1)
            b = localize(boundary)
            ticks.append(Tick(sim_time=b))
            # Day rollover: this tick is 00:00 of a new date (Req 1.6),
            # ordered after the 00:00 tick.
            if b.hour == 0 and b.minute == 0:
                prev_date = (b - timedelta(minutes=1)).date().isoformat()
                rollovers.append(DayRollover(completed_date=prev_date))
        self._last_boundary = boundary

        # Lag warning (Req 1.7): if previous tick batch exceeded budget.
        lag: Optional[LagWarning] = None
        budget = 60.0 / self._accel  # real seconds allotted per tick
        if tick_processing_seconds > budget and ticks:
            if self._can_warn(now):
                lag = LagWarning(backlog_ticks=len(ticks))
                self._last_lag_warn_real = now

        return AdvanceResult(ticks=ticks, day_rollovers=rollovers, lag_warning=lag)

    # -- drift --------------------------------------------------------------
    def drift_seconds(self) -> float:
        """Cumulative drift = exposed sim seconds - accel * running real seconds.

        Excludes paused intervals (Req 1.9). Should stay < 60s per sim day (Req 1.5).
        """
        exposed = (self._sim_time - self._start_sim_time).total_seconds()
        return exposed - self._expected_sim_seconds

    # -- internals ----------------------------------------------------------
    def _now(self) -> float:
        return self._real_clock()

    def _accrue(self, now: float) -> None:
        """Fold accumulated real elapsed into sim time at current accel.

        Used before an accel change or pause so the segment is billed at the
        rate that was actually in effect.
        """
        if self._paused:
            self._real_anchor = now
            return
        elapsed = now - self._real_anchor
        if elapsed < 0:
            elapsed = 0.0
        self._sim_time = self._sim_time + timedelta(seconds=self._accel * elapsed)
        self._running_real_seconds += elapsed
        self._expected_sim_seconds += self._accel * elapsed
        # Re-emit boundaries lazily on next advance(); update anchor only.
        self._real_anchor = now

    def _can_warn(self, now: float) -> bool:
        if self._last_lag_warn_real is None:
            return True
        return (now - self._last_lag_warn_real) >= 10.0


def _valid_accel(factor) -> bool:
    return isinstance(factor, int) and not isinstance(factor, bool) and ACCEL_MIN <= factor <= ACCEL_MAX


__all__ = [
    "Simulation_Clock", "Tick", "DayRollover", "LagWarning", "AdvanceResult",
    "InvalidAccelerationError", "localize", "TIMEZONE",
    "ACCEL_MIN", "ACCEL_MAX", "ACCEL_DEFAULT",
]
