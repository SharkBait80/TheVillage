"""Injected world events — operator-injected happenings + propagation.

Operators can inject world *events* (an explosion, a festival, a lost dog, …)
at a lat/lon/location and sim time. The Simulation_API persists these to
DynamoDB as items with SK ``INJECTED_EVENT#<seq>`` under PK ``SIM#<simId>``.
The engine consumes them once, decides which agents become *aware*, pushes a
memory line into each aware agent's short-term memory, and sets a lightweight
avoidance/attraction hint that biases the heuristic decision engine.

Everything here is pure and deterministic given its inputs (a seeded RNG keyed
on the event id) so behaviour is reproducible and unit-testable.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .movement import haversine_m

# Propagation radii (metres).
LOCAL_RADIUS_M = 300.0     # default "local" event awareness radius
CITY_RADIUS_M = 3000.0     # "city"-scale awareness radius
# Fraction of the (out-of-radius) population that hears a city-scale event
# through word of mouth / media, in addition to those within CITY_RADIUS_M.
CITY_BROADCAST_FRACTION = 0.5

# How long (sim minutes) an avoidance / attraction hint stays in effect.
AVOID_TTL_MIN = 180
ATTRACT_TTL_MIN = 240

# Keyword cues that make an event scary (avoid) or positive (attract).
SCARY_KEYWORDS = (
    "explosion", "explode", "fire", "attack", "gas", "collapse",
    "shooting", "shooter", "riot", "flood", "quake", "bomb", "hazard",
)
POSITIVE_KEYWORDS = (
    "festival", "concert", "market", "sale", "free", "parade",
    "party", "celebration", "fair", "carnival", "fireworks",
)

VALID_SCALES = ("local", "city", "wide")
VALID_SEVERITIES = ("info", "minor", "major", "severe")


@dataclass
class Injected_Event:
    """An operator-injected world event (parsed from a DynamoDB item)."""
    id: str
    simTime: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    locationId: Optional[str] = None
    title: str = ""
    description: str = ""
    scale: str = "local"
    radiusM: Optional[float] = None
    severity: str = "info"
    createdAt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "simTime": self.simTime,
            "lat": self.lat,
            "lon": self.lon,
            "locationId": self.locationId,
            "title": self.title,
            "description": self.description,
            "scale": self.scale,
            "radiusM": self.radiusM,
            "severity": self.severity,
            "createdAt": self.createdAt,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Injected_Event":
        def _f(key: str) -> Optional[float]:
            v = d.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        scale = str(d.get("scale", "local")).lower()
        if scale not in VALID_SCALES:
            scale = "local"
        severity = str(d.get("severity", "info")).lower()
        if severity not in VALID_SEVERITIES:
            severity = "info"
        return cls(
            id=str(d.get("id") or d.get("SK") or ""),
            simTime=str(d.get("simTime", "")),
            lat=_f("lat"),
            lon=_f("lon"),
            locationId=d.get("locationId"),
            title=str(d.get("title", "")),
            description=str(d.get("description", "")),
            scale=scale,
            radiusM=_f("radiusM"),
            severity=severity,
            createdAt=d.get("createdAt"),
        )

    # -- classification -----------------------------------------------------
    def is_scary(self) -> bool:
        blob = f"{self.title} {self.description}".lower()
        if self.severity == "severe":
            return True
        return any(k in blob for k in SCARY_KEYWORDS)

    def is_positive(self) -> bool:
        blob = f"{self.title} {self.description}".lower()
        return any(k in blob for k in POSITIVE_KEYWORDS)


@dataclass
class Propagation_Result:
    """Outcome of propagating one event across the population."""
    event_id: str
    aware_agent_ids: Set[str] = field(default_factory=set)
    memory_line: str = ""
    avoided_location_id: Optional[str] = None
    attractor_location_id: Optional[str] = None
    scary: bool = False
    positive: bool = False

    @property
    def aware_count(self) -> int:
        return len(self.aware_agent_ids)


def _seeded_rng(event_id: str) -> random.Random:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _event_location(event: Injected_Event, world: Any):
    """Resolve the event's (lat, lon), preferring an explicit location."""
    if event.locationId:
        loc = getattr(world, "locations", {}).get(event.locationId)
        if loc is not None:
            return loc.lat, loc.lon
    if event.lat is not None and event.lon is not None:
        return event.lat, event.lon
    return None, None


def _place_name(event: Injected_Event, world: Any) -> str:
    if event.locationId:
        loc = getattr(world, "locations", {}).get(event.locationId)
        if loc is not None:
            return loc.name
    if event.lat is not None and event.lon is not None:
        return f"({event.lat:.4f}, {event.lon:.4f})"
    return "the city"


class Event_Propagation:
    """Decides which agents become aware of an injected event.

    Rules:
      * ``scale == 'wide'`` OR ``severity == 'severe'`` -> ALL agents know.
      * ``scale == 'city'`` -> agents within ``CITY_RADIUS_M`` know, plus a
        deterministic broad subset (word of mouth / media).
      * ``scale == 'local'`` -> only agents within ``radiusM`` (default
        ``LOCAL_RADIUS_M``) of the event location know.

    Also produces a memory line and, for aware agents, an avoidance hint (scary
    events) or attraction hint (positive events).
    """

    def propagate(self, event: Injected_Event, world: Any) -> Propagation_Result:
        agents = list(getattr(world, "agents", {}).values())
        ev_lat, ev_lon = _event_location(event, world)
        place = _place_name(event, world)
        scary = event.is_scary()
        positive = event.is_positive()

        hhmm = event.simTime[11:16] if len(event.simTime) >= 16 else ""
        prefix = f"{hhmm}: " if hhmm else ""
        memory_line = (
            f"{prefix}heard about {event.title or 'an event'} at {place}"
            f": {event.description}".rstrip()
        )

        aware: Set[str] = set()

        everyone = event.scale == "wide" or event.severity == "severe"
        if everyone:
            aware = {a.id for a in agents}
        elif event.scale == "city":
            rng = _seeded_rng(event.id)
            for a in agents:
                within = _within(a, ev_lat, ev_lon, CITY_RADIUS_M)
                if within or rng.random() < CITY_BROADCAST_FRACTION:
                    aware.add(a.id)
        else:  # local (default)
            radius = event.radiusM if event.radiusM and event.radiusM > 0 else LOCAL_RADIUS_M
            for a in agents:
                if _within(a, ev_lat, ev_lon, radius):
                    aware.add(a.id)

        avoided = event.locationId if (scary and event.locationId) else None
        attractor = event.locationId if (positive and not scary and event.locationId) else None

        return Propagation_Result(
            event_id=event.id, aware_agent_ids=aware, memory_line=memory_line,
            avoided_location_id=avoided, attractor_location_id=attractor,
            scary=scary, positive=positive,
        )


def _within(agent: Any, lat: Optional[float], lon: Optional[float],
            radius_m: float) -> bool:
    """True if the agent is within ``radius_m`` of (lat, lon).

    If the event has no coordinates, proximity can't be evaluated -> False
    (such events should be city/wide scale to reach anyone).
    """
    if lat is None or lon is None:
        return False
    st = agent.state
    return haversine_m(st.lat, st.lon, lat, lon) <= radius_m


def add_sim_minutes(sim_iso: str, minutes: int) -> str:
    """Return ``sim_iso`` advanced by ``minutes`` (ISO-8601 in, ISO-8601 out).

    Best-effort: on any parse failure returns the input unchanged so callers
    can treat a hint as "no expiry" rather than crashing a tick.
    """
    from datetime import datetime, timedelta
    try:
        dt = datetime.fromisoformat(sim_iso)
        return (dt + timedelta(minutes=minutes)).isoformat()
    except (ValueError, TypeError):
        return sim_iso


__all__ = [
    "Injected_Event", "Event_Propagation", "Propagation_Result",
    "add_sim_minutes",
    "LOCAL_RADIUS_M", "CITY_RADIUS_M", "CITY_BROADCAST_FRACTION",
    "AVOID_TTL_MIN", "ATTRACT_TTL_MIN",
    "SCARY_KEYWORDS", "POSITIVE_KEYWORDS", "VALID_SCALES", "VALID_SEVERITIES",
]
