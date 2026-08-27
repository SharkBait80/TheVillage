"""Heuristic decision engine — deterministic agent behaviour without an LLM.

When the Agent Harness (AgentCore Runtime) is unavailable, erroring, or
throttled, the engine still needs its agents to *behave* rather than idle
forever. This module provides a pure, deterministic decision function that
produces sensible actions from an agent's needs, cash, employment, the time of
day, reachable locations, and co-located agents.

Design goals:
  * Pure & deterministic per ``(agentId, sim_iso)`` — agents don't all pick the
    same thing, but the same inputs always yield the same output (seeded RNG).
  * Only reference *reachable* location ids / *co-located* agent ids so the
    engine's ``Action.from_dict`` + downstream validation always succeeds.
  * Reliably drive co-located agents to ``socialise`` toward each other so
    conversations actually form even with ``runtime=None``.
  * Respect injected-world-event awareness: avoid locations flagged as
    dangerous (e.g. near an explosion), and optionally gravitate toward
    attractor locations (e.g. a festival).

The returned dict is shaped exactly like ``Action.from_dict`` expects:
``{type, targetType, targetId, expectedDurationMin}`` plus ``crimeType`` only
for ``commit_crime`` (never emitted by the heuristic).
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Optional

# Need thresholds (0..100 scale) that trigger corrective behaviour.
HUNGER_LOW = 35
ENERGY_LOW = 30
SOCIAL_LOW = 40
FUN_LOW = 40
# Energy considered "high enough" that a night-owl need not sleep yet.
ENERGY_HIGH = 60

# Time-of-day boundaries (24h "HH:MM" string comparison is lexicographic-safe).
NIGHT_START = "22:00"
NIGHT_END = "06:00"
WORK_START = "09:00"
WORK_END = "17:00"

# Default expected durations (sim minutes) per action kind.
DUR_SLEEP = 240
DUR_EAT = 30
DUR_WORK = 120
DUR_SOCIALISE = 20
DUR_LEISURE = 45
DUR_TRAVEL = 10
DUR_IDLE = 10

# Location categories used when scoring destinations.
FOOD_CATS = ("food",)
LEISURE_CATS = ("leisure",)
RETAIL_CATS = ("retail",)
RESIDENCE_CATS = ("residence",)
WORKPLACE_CATS = ("workplace",)
# Categories where other agents plausibly congregate (for social mingling).
SOCIAL_CATS = ("leisure", "food", "retail", "civic")


def _seeded_rng(agent_id: str, sim_iso: str) -> random.Random:
    """Deterministic RNG keyed on (agentId, sim_iso).

    Uses a stable hash so behaviour is reproducible across processes/runs
    (Python's builtin ``hash`` is salted per-process and unsuitable here).
    """
    digest = hashlib.sha256(f"{agent_id}|{sim_iso}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _is_night(hhmm: str) -> bool:
    """True if the clock time is night (>= 22:00 or < 06:00)."""
    return hhmm >= NIGHT_START or hhmm < NIGHT_END


def _is_work_hours(hhmm: str) -> bool:
    return WORK_START <= hhmm < WORK_END


def _action(atype: str, target_type: str, target_id: str,
            duration_min: int) -> Dict[str, Any]:
    return {
        "type": atype,
        "targetType": target_type,
        "targetId": target_id,
        "expectedDurationMin": int(duration_min),
    }


def _reachable(world: Any) -> List[Any]:
    """All locations from the world, defensively."""
    locs = getattr(world, "locations", {}) or {}
    return list(locs.values())


def _loc_category(loc: Any) -> str:
    cat = getattr(loc, "category", None)
    return getattr(cat, "value", cat) or ""


def _nearest_in_categories(agent: Any, world: Any, categories: tuple,
                           avoid: Optional[set] = None) -> Optional[str]:
    """Return the id of the nearest reachable location in ``categories``.

    ``avoid`` is a set of location ids to skip (e.g. dangerous places).
    """
    from .movement import haversine_m
    avoid = avoid or set()
    st = agent.state
    best_id: Optional[str] = None
    best_d = float("inf")
    for loc in _reachable(world):
        if loc.id in avoid:
            continue
        if _loc_category(loc) not in categories:
            continue
        d = haversine_m(st.lat, st.lon, loc.lat, loc.lon)
        if d < best_d:
            best_d = d
            best_id = loc.id
    return best_id


def _occupancy_by_location(world: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for other in getattr(world, "agents", {}).values():
        pid = other.state.presentLocationId
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def _busiest_social_location(agent: Any, world: Any,
                             avoid: Optional[set] = None) -> Optional[str]:
    """Reachable social-category location with the most *other* occupants.

    Falls back to the nearest social/leisure location when nowhere is busy.
    """
    avoid = avoid or set()
    counts = _occupancy_by_location(world)
    here = agent.state.presentLocationId
    best_id: Optional[str] = None
    best_score = -1
    for loc in _reachable(world):
        if loc.id in avoid or loc.id == here:
            continue
        if _loc_category(loc) not in SOCIAL_CATS:
            continue
        others = counts.get(loc.id, 0)
        if loc.id == here:
            others = max(0, others - 1)
        if others > best_score:
            best_score = others
            best_id = loc.id
    if best_id is not None and best_score >= 1:
        return best_id
    # Nobody around anywhere reachable -> head to nearest leisure/food to mingle.
    return _nearest_in_categories(agent, world, SOCIAL_CATS, avoid=avoid)


def _colocated_other_ids(agent: Any, world: Any) -> List[str]:
    """Ids of OTHER agents at the same present location (deterministic order)."""
    here = agent.state.presentLocationId
    if not here:
        return []
    out: List[str] = []
    for other in getattr(world, "agents", {}).values():
        if other.id == agent.id:
            continue
        if other.state.presentLocationId == here:
            out.append(other.id)
    out.sort()
    return out


def _present_or_home(agent: Any) -> str:
    return agent.state.presentLocationId or agent.persona.homeLocationId


def _job_location_id(agent: Any, world: Any) -> Optional[str]:
    """Location id of the agent's assigned job, if any."""
    job_id = getattr(agent.state, "jobId", None)
    if not job_id:
        return None
    job = getattr(world, "jobs", {}).get(job_id)
    return getattr(job, "locationId", None) if job else None


def _avoided_location_ids(agent: Any, sim_iso: str) -> set:
    """Location ids the agent is currently avoiding (unexpired hazard hints)."""
    avoided = getattr(agent.state, "avoidedLocations", None) or {}
    live: set = set()
    for loc_id, expiry in avoided.items():
        # expiry is an ISO sim-time string; keep while now < expiry (or no expiry).
        if not expiry or sim_iso < expiry:
            live.add(loc_id)
    return live


def _attractor_location_id(agent: Any, sim_iso: str) -> Optional[str]:
    """A location the agent is currently drawn toward (unexpired), if any."""
    attractor = getattr(agent.state, "attractorLocation", None)
    if not attractor:
        return None
    loc_id = attractor.get("locationId")
    expiry = attractor.get("expiry")
    if loc_id and (not expiry or sim_iso < expiry):
        return loc_id
    return None


def _is_at(agent: Any, location_id: Optional[str]) -> bool:
    return bool(location_id) and agent.state.presentLocationId == location_id


def heuristic_decision(agent: Any, world: Any, sim_iso: str) -> Dict[str, Any]:
    """Return a sensible action dict for ``agent`` at ``sim_iso``.

    Pure and deterministic given ``(agent.id, sim_iso)`` and world state. The
    result is shaped for :meth:`village.models.Action.from_dict`.

    Priority order (roughly by survival need):
      1. Detained agents: restricted to sleep/eat/socialise/idle.
      2. Attractor pull (positive injected event) — sometimes travel toward it.
      3. Night + not wired -> sleep.
      4. Hunger low -> eat / travel to food.
      5. Energy low at home -> sleep.
      6. Work hours + employed + not at work -> travel to / do work.
      7. Social low -> socialise with a co-located agent, else travel to mingle.
      8. Fun low -> leisure / travel to leisure.
      9. Otherwise -> mingle to keep the world lively (avoid idle).
    """
    rng = _seeded_rng(agent.id, sim_iso)
    st = agent.state
    needs = st.needs or {}
    hhmm = sim_iso[11:16] if len(sim_iso) >= 16 else "12:00"
    avoid = _avoided_location_ids(agent, sim_iso)

    legal_status = getattr(st.legalStatus, "value", st.legalStatus)
    home = agent.persona.homeLocationId
    here = _present_or_home(agent)

    # 1. Detained agents may only sleep/eat/socialise/idle at their location.
    if legal_status == "detained":
        colo = _colocated_other_ids(agent, world)
        if needs.get("social", 100) <= SOCIAL_LOW and colo:
            return _action("socialise", "agent", rng.choice(colo), DUR_SOCIALISE)
        if needs.get("hunger", 100) <= HUNGER_LOW:
            return _action("eat", "location", here, DUR_EAT)
        return _action("sleep", "location", here, DUR_SLEEP)

    # 2. Positive injected-event attractor: some agents drift toward it.
    attractor = _attractor_location_id(agent, sim_iso)
    if attractor and attractor not in avoid:
        if _is_at(agent, attractor):
            # Already there: enjoy it / socialise if company present.
            colo = _colocated_other_ids(agent, world)
            if colo and rng.random() < 0.6:
                return _action("socialise", "agent", rng.choice(colo), DUR_SOCIALISE)
            return _action("leisure", "location", attractor, DUR_LEISURE)
        if rng.random() < 0.5:  # not everyone drops everything
            return _action("travel", "location", attractor, DUR_TRAVEL)

    energy = needs.get("energy", 100)
    hunger = needs.get("hunger", 100)
    social = needs.get("social", 100)
    fun = needs.get("fun", 100)

    # 3. Night: sleep at home unless well-rested night owls (deterministic).
    if _is_night(hhmm) and energy < ENERGY_HIGH:
        if _is_at(agent, home) or _loc_category(_loc_of(world, here)) in RESIDENCE_CATS:
            return _action("sleep", "location", here, DUR_SLEEP)
        if home not in avoid:
            return _action("travel", "location", home, DUR_TRAVEL)
        return _action("sleep", "location", here, DUR_SLEEP)

    # 4. Hunger low -> eat here if at food, else travel to nearest food.
    if hunger <= HUNGER_LOW:
        if _loc_category(_loc_of(world, here)) in FOOD_CATS:
            return _action("eat", "location", here, DUR_EAT)
        food = _nearest_in_categories(agent, world, FOOD_CATS, avoid=avoid)
        if food:
            if _is_at(agent, food):
                return _action("eat", "location", food, DUR_EAT)
            return _action("travel", "location", food, DUR_TRAVEL)

    # 5. Energy low and at a residence -> sleep.
    if energy <= ENERGY_LOW:
        if _loc_category(_loc_of(world, here)) in RESIDENCE_CATS or _is_at(agent, home):
            return _action("sleep", "location", here, DUR_SLEEP)
        if home not in avoid:
            return _action("travel", "location", home, DUR_TRAVEL)

    # 6. Work hours + employed + has a workplace -> go to work / work.
    employment = getattr(st.employmentStatus, "value", st.employmentStatus)
    if _is_work_hours(hhmm) and employment == "employed":
        job_loc = _job_location_id(agent, world)
        if job_loc and job_loc not in avoid:
            if _is_at(agent, job_loc):
                return _action("work", "location", job_loc, DUR_WORK)
            return _action("travel", "location", job_loc, DUR_TRAVEL)

    # 7. Social low -> socialise with a co-located agent, else travel to mingle.
    if social <= SOCIAL_LOW:
        colo = _colocated_other_ids(agent, world)
        if colo:
            # Deterministically pick a partner; co-located low-social agents
            # will reciprocate on their own tick, so conversations form.
            return _action("socialise", "agent", rng.choice(colo), DUR_SOCIALISE)
        dest = _busiest_social_location(agent, world, avoid=avoid)
        if dest and not _is_at(agent, dest):
            return _action("travel", "location", dest, DUR_TRAVEL)
        if dest:
            return _action("leisure", "location", dest, DUR_LEISURE)

    # 8. Fun low -> leisure here or travel to nearest leisure.
    if fun <= FUN_LOW:
        if _loc_category(_loc_of(world, here)) in LEISURE_CATS:
            return _action("leisure", "location", here, DUR_LEISURE)
        leisure = _nearest_in_categories(agent, world, LEISURE_CATS, avoid=avoid)
        if leisure:
            if _is_at(agent, leisure):
                return _action("leisure", "location", leisure, DUR_LEISURE)
            return _action("travel", "location", leisure, DUR_TRAVEL)

    # 9. Otherwise: keep the world lively. Prefer socialising with company,
    #    else mingle toward a busy/leisure spot, else leisure in place.
    colo = _colocated_other_ids(agent, world)
    if colo and rng.random() < 0.5:
        return _action("socialise", "agent", rng.choice(colo), DUR_SOCIALISE)
    dest = _busiest_social_location(agent, world, avoid=avoid)
    if dest and not _is_at(agent, dest):
        return _action("travel", "location", dest, DUR_TRAVEL)
    if dest:
        return _action("leisure", "location", dest, DUR_LEISURE)

    # True last resort: idle in place.
    return _action("idle", "location", here, DUR_IDLE)


def _loc_of(world: Any, location_id: Optional[str]) -> Any:
    if not location_id:
        return None
    return getattr(world, "locations", {}).get(location_id)


# -- local utterance fallback (no harness) ---------------------------------
_GREETINGS = ("G'day", "Hi", "Hello", "Hey there", "Morning")
_SMALL_TALK = (
    "how's your day going?",
    "busy around here today.",
    "lovely spot, isn't it?",
    "long time no see.",
    "what brings you here?",
)


def local_utterance(speaker_persona: Any, location_name: str,
                    turn_index: int, sim_iso: str,
                    memory_lines: Optional[List[str]] = None) -> str:
    """Deterministic, persona-flavoured small-talk line (no LLM needed).

    Keeps conversations flowing when the harness is unavailable. If the speaker
    has a recent memory referencing an injected world event, the line refers to
    it so agents "talk about" the explosion/festival/etc.
    """
    name = getattr(speaker_persona, "name", "Someone")
    occupation = getattr(speaker_persona, "occupation", "local")
    seed = f"{name}|{sim_iso}|{turn_index}"
    rng = _seeded_rng(seed, sim_iso)

    # If a memory mentions an injected event, reference it sometimes.
    event_line = _event_talk(memory_lines, rng)
    if event_line and rng.random() < 0.7:
        return event_line[:500]

    if turn_index == 0:
        greeting = rng.choice(_GREETINGS)
        return f"{greeting}! {rng.choice(_SMALL_TALK)}"[:500]
    talk = rng.choice(_SMALL_TALK)
    flavour = rng.choice((
        f"Anyway, as a {occupation}, I keep busy.",
        f"Nice to run into you at {location_name}.",
        "Take care of yourself.",
        "We should catch up more often.",
    ))
    return f"{talk} {flavour}"[:500]


def _event_talk(memory_lines: Optional[List[str]], rng: random.Random) -> Optional[str]:
    """Extract a conversational line about a remembered injected event."""
    if not memory_lines:
        return None
    for line in reversed(memory_lines):
        low = line.lower()
        if "heard about" in low:
            # memory format: "HH:MM: heard about <title> at <place>: <desc>"
            fragment = line.split("heard about", 1)[1].strip()
            title = fragment.split(":", 1)[0].strip()
            opener = rng.choice((
                "Did you hear about",
                "Can you believe",
                "Everyone's talking about",
            ))
            return f"{opener} {title}? Wild, isn't it."
    return None


__all__ = ["heuristic_decision", "local_utterance"]
