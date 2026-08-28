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

# Minimum turns the LLM-free conversation fallback plays out so an exchange has
# a greeting, at least one responsive middle turn, and a natural close.
MIN_LOCAL_TURNS = 4

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


# --------------------------------------------------------------------------- #
# Myers-Briggs (MBTI) personality helpers.
#
# An agent's 4-letter type biases its behaviour along four axes:
#   E/I  Extraversion vs Introversion  -> how eagerly it seeks company
#   S/N  Sensing vs Intuition          -> conversational flavour (concrete vs ideas)
#   T/F  Thinking vs Feeling           -> conversational flavour (facts vs warmth)
#   J/P  Judging vs Perceiving         -> structure vs spontaneity of activities
# All helpers are pure and tolerate a missing/blank type (neutral behaviour).
# --------------------------------------------------------------------------- #

def _mbti_of(agent: Any) -> str:
    persona = getattr(agent, "persona", None)
    mbti = getattr(persona, "mbti", "") if persona is not None else ""
    return (mbti or "").upper()


def _is_extravert(mbti: str) -> bool:
    return bool(mbti) and mbti[0] == "E"


def _is_introvert(mbti: str) -> bool:
    return bool(mbti) and mbti[0] == "I"


def _is_judging(mbti: str) -> bool:
    return len(mbti) >= 4 and mbti[3] == "J"


def _is_perceiving(mbti: str) -> bool:
    return len(mbti) >= 4 and mbti[3] == "P"


def _social_threshold_for(mbti: str) -> int:
    """Effective 'social need is low' threshold for this personality.

    Extraverts feel the pull of company sooner (higher threshold => triggers
    earlier); introverts tolerate more solitude before seeking others.
    """
    if _is_extravert(mbti):
        return SOCIAL_LOW + 20
    if _is_introvert(mbti):
        return SOCIAL_LOW - 15
    return SOCIAL_LOW


def _mingle_probability(mbti: str) -> float:
    """Baseline chance an otherwise-satisfied agent chooses to mingle/socialise."""
    if _is_extravert(mbti):
        return 0.75
    if _is_introvert(mbti):
        return 0.25
    return 0.5


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
    mbti = _mbti_of(agent)
    social_low = _social_threshold_for(mbti)
    mingle_p = _mingle_probability(mbti)

    legal_status = getattr(st.legalStatus, "value", st.legalStatus)
    home = agent.persona.homeLocationId
    here = _present_or_home(agent)

    # 1. Detained agents may only sleep/eat/socialise/idle at their location.
    if legal_status == "detained":
        colo = _colocated_other_ids(agent, world)
        if needs.get("social", 100) <= social_low and colo:
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
            if colo and rng.random() < mingle_p:
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
    #    The threshold is personality-adjusted: extraverts seek company sooner,
    #    introverts tolerate more solitude first (MBTI E/I).
    if social <= social_low:
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

    # 9. Otherwise: keep the world lively. Extraverts strongly prefer company,
    #    introverts lean toward solo leisure (MBTI E/I via ``mingle_p``).
    colo = _colocated_other_ids(agent, world)
    if colo and rng.random() < mingle_p:
        return _action("socialise", "agent", rng.choice(colo), DUR_SOCIALISE)
    # Judging types like a purposeful outing (travel to a destination);
    # perceiving types are happy to relax where they are (MBTI J/P).
    dest = _busiest_social_location(agent, world, avoid=avoid)
    if dest and not _is_at(agent, dest) and not (_is_perceiving(mbti) and rng.random() < 0.5):
        return _action("travel", "location", dest, DUR_TRAVEL)
    if dest:
        return _action("leisure", "location", dest, DUR_LEISURE)
    if _is_at(agent, here):
        return _action("leisure", "location", here, DUR_LEISURE)

    # True last resort: idle in place.
    return _action("idle", "location", here, DUR_IDLE)


def _loc_of(world: Any, location_id: Optional[str]) -> Any:
    if not location_id:
        return None
    return getattr(world, "locations", {}).get(location_id)


# -- local utterance fallback (no harness) ---------------------------------
#
# The LLM-free fallback must still read like a real, flowing conversation:
# each turn responds to what the partner just said, the pair stays on ONE
# topic, and lines progress greeting -> topic -> back-and-forth -> close
# without ever repeating verbatim. We model this as a small adjacency-pair
# dialogue state machine (see AIIDE "Talking with NPCs"; Frontiers in AI
# frai.2025.1582287 on adjacency pairs + response obligation; Stanford
# Generative Agents arXiv:2304.03442 on grounding each utterance on the
# partner's last line). Determinism is preserved by keying every choice on a
# stable conversation id + turn index rather than a per-process RNG.

_GREETINGS = ("G'day", "Hi", "Hello", "Hey there", "Morning")

# One shared topic per conversation, chosen deterministically. Each topic
# carries an opener (raised by the first speaker), a menu of on-topic
# developments, and follow-up questions so the thread can actually progress.
# Keyed by MBTI axis (S/N for concrete-vs-ideas, T/F for facts-vs-warmth) so an
# agent's personality colours WHICH topic surfaces and HOW it is discussed.
_TOPICS_BY_AXIS = {
    "N": (
        {"opener": "I've been dreaming up a new project lately.",
         "develop": ("It could really change how people gather round here.",
                     "Half the fun is imagining where it could go.",
                     "I keep sketching out ideas for it at night."),
         "ask": ("Ever get an idea you just can't shake?",
                 "What would you build if nothing held you back?")},
        {"opener": "Do you ever wonder what this city will look like in ten years?",
         "develop": ("I picture the laneways greener, somehow livelier.",
                     "Everything's changing so fast it's hard to keep up."),
         "ask": ("Where do you reckon it's all heading?",)},
    ),
    "S": (
        {"opener": "The coffee here's been spot on this week.",
         "develop": ("I come by most mornings before the rush.",
                     "They finally fixed the machine, you can taste it."),
         "ask": ("Have you tried the new roast yet?",
                 "What's your usual order?")},
        {"opener": "The tram was right on time today, believe it or not.",
         "develop": ("Made the whole morning run smoother.",
                     "Rare enough that I noticed."),
         "ask": ("How was your trip in?",)},
    ),
    "F": (
        {"opener": "It's so good to run into a friendly face.",
         "develop": ("These little catch-ups always lift my mood.",
                     "I've been thinking about how you're getting on."),
         "ask": ("How are you really doing, though?",
                 "Is everything alright with you lately?")},
    ),
    "T": (
        {"opener": "I've been mulling over a tricky problem at work.",
         "develop": ("There's a cleaner way to solve it, I'm sure of it.",
                     "The numbers only add up if I rework the plan."),
         "ask": ("Makes sense to plan the week out, don't you think?",
                 "How would you approach something like that?")},
    ),
}

# Second-pair-part templates. When the partner just asked a question we ANSWER;
# when they made a statement we ACKNOWLEDGE before adding our own thread.
_ANSWERS = (
    "Honestly, not bad at all — keeping busy.",
    "Can't complain, plenty on but I'm managing.",
    "Good question — I've been turning that over myself.",
    "Better than last week, that's for sure.",
)
_ACKS = (
    "Fair point.",
    "I know exactly what you mean.",
    "Ha, tell me about it.",
    "Right? I was just thinking the same.",
    "That's the truth.",
)
_CLOSINGS = (
    "Anyway, I'd best get on — take care, {partner}.",
    "Good to see you, {partner}. Let's catch up properly soon.",
    "I'll let you get on with your day, {partner}.",
    "Lovely chatting, {partner} — see you around.",
)

# Generic follow-up questions used to keep the exchange varied when a topic
# offers only a single scripted question.
_FOLLOW_UPS = (
    "What do you make of it?",
    "How's that sitting with you?",
    "Anything else on your plate this week?",
    "You been keeping well otherwise?",
)

# Short MBTI-flavoured connectors appended to middle turns so an agent's
# personality colours HOW it speaks even while both partners share ONE topic.
# Keyed on the T/F axis (warmth vs pragmatism) and E/I (effusive vs terse).
_FLAVOUR_BY_AXIS = {
    "F": ("Means a lot, honestly.", "So glad we crossed paths.",
          "You always know what to say."),
    "T": ("Practically speaking, anyway.", "Worth thinking through.",
          "That's my read on it."),
    "E": ("Love a good natter, me.", "We should do this more often.",
          "Always good to swap notes."),
    "I": ("Just my quiet take.", "Anyway.", "For what it's worth."),
}


def _persona_field(persona: Any, field: str, default: str = "") -> str:
    return getattr(persona, field, default) or default


def _axis_topics(mbti: str) -> List[Dict[str, Any]]:
    """Topic pool coloured by the speaker's MBTI (S/N then T/F)."""
    pools: List[Dict[str, Any]] = []
    if len(mbti) >= 3:
        pools += list(_TOPICS_BY_AXIS.get(mbti[1], ()))
        pools += list(_TOPICS_BY_AXIS.get(mbti[2], ()))
    if not pools:
        # Neutral fallback for a missing/blank type.
        for v in _TOPICS_BY_AXIS.values():
            pools += list(v)
    return pools


def _all_topics() -> List[Dict[str, Any]]:
    """Every topic, in a stable order — used to pick the ONE shared topic for a
    conversation so both partners (whatever their MBTI) stay on one thread."""
    pools: List[Dict[str, Any]] = []
    for key in ("N", "S", "F", "T"):
        pools += list(_TOPICS_BY_AXIS.get(key, ()))
    return pools


def _conv_seed(conv_id: str, extra: str = "") -> int:
    """Stable integer derived from the conversation id (process-independent)."""
    digest = hashlib.sha256(f"{conv_id}|{extra}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _pick(pool, conv_id: str, salt: str, turn_index: int):
    """Deterministic rotation over ``pool`` — never repeats back-to-back and
    is stable across processes for a given (conv_id, salt, turn)."""
    seq = list(pool)
    if not seq:
        return None
    idx = (_conv_seed(conv_id, salt) + turn_index) % len(seq)
    return seq[idx]


def _conv_topic(conv_id: str, mbti: str = "") -> Dict[str, Any]:
    """The single shared topic for this conversation.

    Keyed purely on ``conv_id`` so BOTH participants derive the same topic
    regardless of their (differing) MBTI types — the exchange stays on one
    thread instead of splitting into two disconnected monologues.
    """
    pool = _all_topics()
    return pool[_conv_seed(conv_id, "topic") % len(pool)]


def local_utterance(speaker_persona: Any, location_name: str,
                    turn_index: int, sim_iso: str,
                    memory_lines: Optional[List[str]] = None,
                    partner_name: Optional[str] = None,
                    conv_id: Optional[str] = None,
                    total_turns: int = 4,
                    partner_last_line: Optional[str] = None) -> str:
    """Deterministic, persona-flavoured conversational line (no LLM needed).

    Produces a coherent back-and-forth by (a) grounding each reply on the
    partner's previous line (answering questions, acknowledging statements),
    (b) holding ONE shared topic for the whole conversation, and (c) walking a
    greeting -> topic -> respond -> wind-down -> close state machine. Choices
    are keyed on a stable ``conv_id`` so both speakers agree on the topic and
    nothing repeats verbatim, while remaining fully deterministic.

    Agents address each other by ``partner_name`` (never "agent"). If a recent
    memory references an injected world event, the opener/topic can pivot to it
    so agents "talk about" the explosion/festival/etc.
    """
    name = _persona_field(speaker_persona, "name", "Someone")
    mbti = _persona_field(speaker_persona, "mbti").upper()
    partner = (partner_name or "").strip().split(" ")[0] if partner_name else ""
    # A stable id so both participants derive the same topic even without one
    # supplied by the caller (older callers pass none).
    cid = conv_id or f"{sim_iso}|{location_name}"
    total = max(MIN_LOCAL_TURNS, total_turns)

    topic = _conv_topic(cid)

    # An injected-event memory outranks small talk as the conversation topic.
    event_line = _event_talk(memory_lines, _seeded_rng(cid, sim_iso), partner)

    # Conversation-state machine keyed on the turn position.
    #   0            greeting + raise the topic (or the remembered event)
    #   last turn    natural closing
    #   otherwise    respond to the partner, then develop / ask
    if turn_index == 0:
        greeting = _pick(_GREETINGS, cid, f"greet|{name}", turn_index)
        who = f" {partner}" if partner else ""
        opener = event_line if (event_line and turn_index == 0) else topic["opener"]
        return f"{greeting}{who}! {opener}"[:500]

    if turn_index >= total - 1:
        closing = _pick(_CLOSINGS, cid, f"close|{name}", turn_index)
        return closing.format(partner=partner or "you")[:500]

    # Middle turns: ground on what the partner just said, then move the
    # conversation forward with an on-topic development or a follow-up question.
    parts: List[str] = []
    last = (partner_last_line or "").strip()
    if last.endswith("?"):
        parts.append(_pick(_ANSWERS, cid, f"ans|{name}", turn_index))
    elif last:
        parts.append(_pick(_ACKS, cid, f"ack|{name}", turn_index))

    # Alternate between developing the topic and asking a follow-up so the
    # thread breathes instead of stalling. Salt on the speaker + a rotating
    # offset so a small topic pool doesn't surface the same line twice.
    if turn_index % 2 == 1 and topic.get("ask"):
        # A given speaker asks on turns 1,3,5…; dividing by two advances the
        # rotation by one each time so a 2-item ask pool doesn't alternate back
        # onto the same question.
        follow = _pick(topic["ask"], cid, f"ask|{name}", turn_index // 2)
        # If this topic offers only one question, vary later asks with a
        # generic follow-up so the exchange never repeats verbatim.
        if turn_index > 1 and len(topic["ask"]) < 2:
            follow = _pick(_FOLLOW_UPS, cid, f"fup|{name}", turn_index // 2)
        parts.append(follow)
    else:
        parts.append(_pick(topic["develop"], cid, f"dev|{name}", turn_index))
        # Colour a development turn with an MBTI-flavoured aside so personality
        # shows even on a shared topic (keyed T/F, then E/I).
        flavour_pool = []
        if len(mbti) >= 3:
            flavour_pool += list(_FLAVOUR_BY_AXIS.get(mbti[2], ()))
        if mbti:
            flavour_pool += list(_FLAVOUR_BY_AXIS.get(mbti[0], ()))
        if flavour_pool:
            parts.append(_pick(flavour_pool, cid, f"flav|{name}", turn_index))

    return " ".join(p for p in parts if p)[:500]


def _event_talk(memory_lines: Optional[List[str]], rng: random.Random,
                partner: str = "") -> Optional[str]:
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
            who = f", {partner}" if partner else ""
            return f"{opener} {title}{who}? Wild, isn't it."
    return None


__all__ = ["heuristic_decision", "local_utterance", "in_world_reason",
           "MIN_LOCAL_TURNS"]


# In-world, player-facing justification for a chosen action. This is what the
# UI's decision trail shows, so it must read like the agent's own thought — it
# must NEVER reference the engine, the LLM, or any implementation detail.
_REASON_BY_ACTION: Dict[str, tuple] = {
    "sleep": (
        "I'm worn out — time to rest.",
        "Feeling drained; I need some sleep.",
        "It's late and I'm tired, so I'm turning in.",
    ),
    "eat": (
        "I'm hungry, so I'm getting something to eat.",
        "Time for a bite — my stomach's rumbling.",
        "Could really go for a meal right now.",
    ),
    "work": (
        "I've got work to do, so I'm getting on with it.",
        "It's my shift — best get to it.",
        "Time to earn my keep.",
    ),
    "travel": (
        "Heading somewhere I need to be.",
        "Time to make my way across town.",
        "On the move to where I'm headed next.",
    ),
    "socialise": (
        "I could use some company, so I'm saying hello.",
        "Feeling a bit lonely — nice to chat with someone.",
        "Good to catch up with a familiar face.",
    ),
    "leisure": (
        "I've earned a little downtime.",
        "Time to relax and enjoy myself.",
        "Fancy a bit of fun to unwind.",
    ),
    "shop": (
        "Popping out to pick up a few things.",
        "Time to do a spot of shopping.",
        "Need to grab a couple of bits.",
    ),
    "idle": (
        "Just taking a quiet moment.",
        "Nothing pressing right now — happy to pause.",
        "Content to stay put for a bit.",
    ),
}


def in_world_reason(action_type: str, agent_id: str, sim_iso: str) -> str:
    """A short, in-character reason for the agent's action (UI-facing).

    Deterministic given ``(agent_id, sim_iso, action_type)`` so replays are
    stable. Never mentions the engine, heuristics, or the LLM.
    """
    options = _REASON_BY_ACTION.get(action_type) or _REASON_BY_ACTION["idle"]
    rng = _seeded_rng(f"{agent_id}|reason|{action_type}", sim_iso)
    return rng.choice(options)
