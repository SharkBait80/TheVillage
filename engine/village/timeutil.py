"""Time & location-status helpers (Requirement 3 status, shared).

Determines a Location's open/closed status against a Melbourne-local
Simulated_Time, and parses HH:MM shift/opening times.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from .clock import localize
from .models import Location, LocationCategory, LocationStatus, OpeningHours


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def hhmm_minutes(s: str) -> int:
    h, m = parse_hhmm(s)
    return h * 60 + m


def day_index(dt: datetime) -> int:
    """0=Monday .. 6=Sunday, matching DESIGN.md hours indexing."""
    return localize(dt).weekday()


def is_open_at(loc: Location, sim_time: datetime) -> bool:
    """True if within opening hours for the current Melbourne day (Req 3.4/3.10)."""
    dt = localize(sim_time)
    idx = dt.weekday()
    if not loc.hours or idx >= len(loc.hours):
        return False
    oh = loc.hours[idx]
    open_min = hhmm_minutes(oh.open)
    close_min = hhmm_minutes(oh.close)
    now_min = dt.hour * 60 + dt.minute
    if close_min <= open_min:
        # overnight hours (e.g. 20:00-02:00): open if after open OR before close
        return now_min >= open_min or now_min < close_min
    return open_min <= now_min < close_min


def location_status(loc: Location, sim_time: datetime, occupancy: int) -> LocationStatus:
    """Report open / closed / at_capacity (Req 3.4/3.5/3.10)."""
    if not is_open_at(loc, sim_time):
        return LocationStatus.CLOSED
    if occupancy >= loc.capacity:
        return LocationStatus.AT_CAPACITY
    return LocationStatus.OPEN


def next_opening(loc: Location, sim_time: datetime,
                 within_days: int = 7) -> Optional[datetime]:
    """Next Simulated_Time this location opens, within `within_days` (Req 3.6/8.7).

    Returns None if it has no opening in that window.
    """
    dt = localize(sim_time)
    # If open right now, the "next opening" is now.
    if is_open_at(loc, dt):
        return dt
    # Scan minute-of-day boundaries across the window. To stay cheap we scan
    # each day's opening time.
    for day_offset in range(0, within_days + 1):
        probe_date = (dt + timedelta(days=day_offset))
        idx = probe_date.weekday()
        if not loc.hours or idx >= len(loc.hours):
            continue
        oh = loc.hours[idx]
        open_h, open_m = parse_hhmm(oh.open)
        candidate = probe_date.replace(hour=open_h, minute=open_m,
                                       second=0, microsecond=0)
        candidate = localize(candidate)
        if candidate >= dt and is_open_at(loc, candidate):
            return candidate
    return None


__all__ = [
    "parse_hhmm", "hhmm_minutes", "day_index",
    "is_open_at", "location_status", "next_opening",
]
