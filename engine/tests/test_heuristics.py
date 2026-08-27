"""Tests for the deterministic heuristic decision engine (village/heuristics.py)."""
from village.heuristics import heuristic_decision, local_utterance
from village.models import (Action, AgentState, Config, EmploymentStatus, Job,
                            LegalStatus, Location, LocationCategory,
                            OpeningHours, Persona, Agent)
from village.ticker import WorldState

H247 = [OpeningHours("00:00", "23:59")] * 7


def _loc(lid, name, cat, lat, lon, price=None):
    return Location(id=lid, name=name, category=cat, lat=lat, lon=lon,
                    capacity=20, hours=H247, price=price)


def _agent(aid, lat, lon, present, needs, *, employment=EmploymentStatus.UNEMPLOYED,
           job_id=None, legal=LegalStatus.CLEAR, home="loc_home"):
    st = AgentState(lat=lat, lon=lon, presentLocationId=present, needs=needs,
                    cash=100.0, employmentStatus=employment, legalStatus=legal,
                    jobId=job_id, dailyLivingCost=40.0)
    persona = Persona(name=f"A{aid}", age=30, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId=home)
    return Agent(id=aid, persona=persona, state=st)


def _world(agents, locations, jobs=None):
    return WorldState(config=Config(simId="melb"),
                      agents={a.id: a for a in agents},
                      locations={l.id: l for l in locations},
                      jobs={j.id: j for j in (jobs or [])})


def _base_locations():
    return [
        _loc("loc_home", "Home", LocationCategory.RESIDENCE, -37.810, 144.950),
        _loc("loc_cafe", "Cafe", LocationCategory.FOOD, -37.812, 144.952, price=8.5),
        _loc("loc_park", "Park", LocationCategory.LEISURE, -37.815, 144.955),
        _loc("loc_shop", "Shop", LocationCategory.RETAIL, -37.813, 144.953, price=12.0),
        _loc("loc_work", "Office", LocationCategory.WORKPLACE, -37.811, 144.951),
    ]


def test_action_dict_shape_is_valid_for_action_from_dict():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 70, "social": 70, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    # Must round-trip through Action.from_dict without raising.
    action = Action.from_dict({**d, "startedSimTime": "2026-03-02T12:00:00+11:00"})
    assert action.type is not None
    assert action.targetId  # non-empty


def test_deterministic_same_inputs_same_output():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 70, "social": 30, "fun": 70})
    world = _world([a], locs)
    sim = "2026-03-02T12:00:00+11:00"
    d1 = heuristic_decision(a, world, sim)
    d2 = heuristic_decision(a, world, sim)
    assert d1 == d2


def test_night_low_energy_sleeps_at_home():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 40, "social": 70, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T23:30:00+11:00")
    assert d["type"] == "sleep"
    assert d["targetId"] == "loc_home"


def test_low_hunger_travels_to_food_when_not_at_food():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 20, "energy": 70, "social": 70, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] == "travel"
    assert d["targetId"] == "loc_cafe"


def test_low_hunger_eats_when_at_food():
    locs = _base_locations()
    a = _agent("agent_01", -37.812, 144.952, "loc_cafe",
               {"hunger": 20, "energy": 70, "social": 70, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] == "eat"
    assert d["targetId"] == "loc_cafe"


def test_low_energy_at_home_sleeps():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 20, "social": 70, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] == "sleep"


def test_work_hours_employed_travels_to_workplace():
    locs = _base_locations()
    job = Job(id="job_1", locationId="loc_work", occupation="Clerk",
              wagePerHour=30.0, shiftStart="09:00", shiftDurationHours=8)
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 70, "social": 70, "fun": 70},
               employment=EmploymentStatus.EMPLOYED, job_id="job_1")
    world = _world([a], locs, jobs=[job])
    d = heuristic_decision(a, world, "2026-03-02T10:00:00+11:00")
    assert d["type"] == "travel"
    assert d["targetId"] == "loc_work"


def test_work_when_at_workplace():
    locs = _base_locations()
    job = Job(id="job_1", locationId="loc_work", occupation="Clerk",
              wagePerHour=30.0, shiftStart="09:00", shiftDurationHours=8)
    a = _agent("agent_01", -37.811, 144.951, "loc_work",
               {"hunger": 70, "energy": 70, "social": 70, "fun": 70},
               employment=EmploymentStatus.EMPLOYED, job_id="job_1")
    world = _world([a], locs, jobs=[job])
    d = heuristic_decision(a, world, "2026-03-02T10:00:00+11:00")
    assert d["type"] == "work"
    assert d["targetId"] == "loc_work"


def test_low_social_with_colocated_agent_socialises_toward_them():
    locs = _base_locations()
    a1 = _agent("agent_01", -37.815, 144.955, "loc_park",
                {"hunger": 70, "energy": 70, "social": 20, "fun": 70})
    a2 = _agent("agent_02", -37.815, 144.955, "loc_park",
                {"hunger": 70, "energy": 70, "social": 70, "fun": 70})
    world = _world([a1, a2], locs)
    d = heuristic_decision(a1, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] == "socialise"
    assert d["targetType"] == "agent"
    assert d["targetId"] == "agent_02"


def test_low_social_alone_travels_to_mingle():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 70, "social": 20, "fun": 70})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    # Alone -> should travel to a social location (not idle).
    assert d["type"] in ("travel", "leisure")
    assert d["targetId"] != "loc_home" or d["type"] == "leisure"


def test_low_fun_goes_to_leisure():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 70, "energy": 70, "social": 70, "fun": 20})
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] == "travel"
    assert d["targetId"] == "loc_park"


def test_detained_agent_restricted_actions():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_remand",
               {"hunger": 70, "energy": 70, "social": 70, "fun": 70},
               legal=LegalStatus.DETAINED)
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["type"] in ("sleep", "eat", "socialise", "idle")


def test_avoids_flagged_location():
    locs = _base_locations()
    a = _agent("agent_01", -37.810, 144.950, "loc_home",
               {"hunger": 20, "energy": 70, "social": 70, "fun": 70})
    # Mark the only food location as avoided (unexpired) — heuristic must not
    # travel there.
    a.state.avoidedLocations = {"loc_cafe": "2026-03-02T18:00:00+11:00"}
    world = _world([a], locs)
    d = heuristic_decision(a, world, "2026-03-02T12:00:00+11:00")
    assert d["targetId"] != "loc_cafe"


def test_attractor_pulls_agent_toward_positive_event():
    locs = _base_locations()
    # Give many agents the same attractor and confirm at least some travel to it
    # (probabilistic 0.5 per-agent, but deterministic per id).
    agents = []
    for i in range(10):
        a = _agent(f"agent_{i:02d}", -37.810, 144.950, "loc_home",
                   {"hunger": 70, "energy": 70, "social": 70, "fun": 70})
        a.state.attractorLocation = {"locationId": "loc_park",
                                     "expiry": "2026-03-02T18:00:00+11:00"}
        agents.append(a)
    world = _world(agents, locs)
    dests = [heuristic_decision(a, world, "2026-03-02T12:00:00+11:00") for a in agents]
    travels_to_park = [d for d in dests if d["type"] == "travel" and d["targetId"] == "loc_park"]
    assert travels_to_park, "expected at least one agent drawn to the attractor"


def test_local_utterance_is_nonempty_and_bounded():
    persona = Persona(name="Aroha", age=34, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    line0 = local_utterance(persona, "the Park", 0, "2026-03-02T12:00:00+11:00")
    line1 = local_utterance(persona, "the Park", 1, "2026-03-02T12:00:00+11:00")
    assert isinstance(line0, str) and line0.strip()
    assert isinstance(line1, str) and line1.strip()
    assert len(line0) <= 500 and len(line1) <= 500


def test_local_utterance_references_remembered_event():
    persona = Persona(name="Aroha", age=34, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    mem = ["12:00: heard about Explosion downtown at Cafe: a loud blast shook the block"]
    # Try several turns to defeat the 0.7 gate deterministically across indices.
    lines = [local_utterance(persona, "Cafe", i, "2026-03-02T12:00:00+11:00",
                             memory_lines=mem) for i in range(8)]
    assert any("Explosion" in ln for ln in lines), \
        "expected at least one utterance to reference the remembered event"
