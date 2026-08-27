"""Tests for injected world events + propagation (village/events_inject.py)."""
from village.events_inject import (Event_Propagation, Injected_Event,
                                   add_sim_minutes, CITY_RADIUS_M, LOCAL_RADIUS_M)
from village.models import (AgentState, Config, EmploymentStatus, LegalStatus,
                            Location, LocationCategory, OpeningHours, Persona,
                            Agent)
from village.ticker import WorldState

H247 = [OpeningHours("00:00", "23:59")] * 7


def _agent(aid, lat, lon, present="loc_home"):
    st = AgentState(lat=lat, lon=lon, presentLocationId=present,
                    needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                    cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                    legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name=f"A{aid}", age=30, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    return Agent(id=aid, persona=persona, state=st)


def _world(agents, locations=None):
    return WorldState(config=Config(simId="melb"),
                      agents={a.id: a for a in agents},
                      locations={l.id: l for l in (locations or [])})


def test_from_dict_normalises_invalid_scale_and_severity():
    ev = Injected_Event.from_dict({
        "id": "e1", "simTime": "2026-03-02T12:00:00+11:00",
        "title": "Thing", "scale": "galactic", "severity": "apocalyptic",
    })
    assert ev.scale == "local"
    assert ev.severity == "info"


def test_to_from_dict_roundtrip():
    ev = Injected_Event(id="e1", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, locationId="loc_x",
                        title="Festival", description="fun", scale="city",
                        radiusM=500.0, severity="minor", createdAt="now")
    again = Injected_Event.from_dict(ev.to_dict())
    assert again == ev


def test_scary_and_positive_classification():
    explosion = Injected_Event(id="e", simTime="", title="Explosion",
                               description="a gas blast", scale="local")
    festival = Injected_Event(id="f", simTime="", title="Street Festival",
                              description="free music", scale="city")
    severe = Injected_Event(id="s", simTime="", title="Odd thing",
                            description="unknown", scale="local", severity="severe")
    assert explosion.is_scary()
    assert festival.is_positive()
    assert severe.is_scary()  # severity alone makes it scary
    assert not festival.is_scary()


def test_wide_scale_makes_all_agents_aware():
    agents = [_agent(f"a{i}", -37.99, 145.09) for i in range(5)]  # far away
    world = _world(agents)
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, title="City-wide alert",
                        scale="wide", severity="major")
    res = Event_Propagation().propagate(ev, world)
    assert res.aware_count == 5


def test_severe_severity_makes_all_agents_aware():
    agents = [_agent(f"a{i}", -37.99, 145.09) for i in range(4)]
    world = _world(agents)
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, title="Explosion",
                        description="huge blast", scale="local", severity="severe")
    res = Event_Propagation().propagate(ev, world)
    assert res.aware_count == 4
    assert res.scary is True


def test_local_scale_only_nearby_agents_aware():
    near = _agent("near", -37.8101, 144.9501)   # ~metres from event
    far = _agent("far", -37.95, 145.05)          # kilometres away
    world = _world([near, far])
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, title="Lost dog",
                        description="small terrier", scale="local",
                        radiusM=LOCAL_RADIUS_M)
    res = Event_Propagation().propagate(ev, world)
    assert "near" in res.aware_agent_ids
    assert "far" not in res.aware_agent_ids


def test_city_scale_nearby_always_aware():
    near = _agent("near", -37.8102, 144.9502)
    world = _world([near])
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, title="Parade", scale="city")
    res = Event_Propagation().propagate(ev, world)
    assert "near" in res.aware_agent_ids


def test_scary_event_sets_avoided_location():
    near = _agent("near", -37.8101, 144.9501)
    world = _world([near])
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, locationId="loc_danger",
                        title="Explosion", description="blast", scale="wide")
    res = Event_Propagation().propagate(ev, world)
    assert res.avoided_location_id == "loc_danger"
    assert res.attractor_location_id is None


def test_positive_event_sets_attractor_location():
    near = _agent("near", -37.8101, 144.9501)
    world = _world([near])
    ev = Injected_Event(id="e", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, locationId="loc_fest",
                        title="Festival", description="free concert", scale="wide")
    res = Event_Propagation().propagate(ev, world)
    assert res.attractor_location_id == "loc_fest"
    assert res.avoided_location_id is None


def test_memory_line_contains_title_and_description():
    agents = [_agent("a", -37.81, 144.95)]
    loc = Location(id="loc_sq", name="City Square", category=LocationCategory.CIVIC,
                   lat=-37.81, lon=144.95, capacity=50, hours=H247)
    world = _world(agents, [loc])
    ev = Injected_Event(id="e", simTime="2026-03-02T14:30:00+11:00",
                        locationId="loc_sq", title="Festival",
                        description="live music", scale="wide")
    res = Event_Propagation().propagate(ev, world)
    assert "Festival" in res.memory_line
    assert "live music" in res.memory_line
    assert "City Square" in res.memory_line
    assert res.memory_line.startswith("14:30:")


def test_add_sim_minutes_advances_iso_time():
    out = add_sim_minutes("2026-03-02T12:00:00+11:00", 180)
    assert out.startswith("2026-03-02T15:00:00")


def test_add_sim_minutes_bad_input_returns_input():
    assert add_sim_minutes("not-a-time", 30) == "not-a-time"


def test_propagation_is_deterministic():
    agents = [_agent(f"a{i}", -37.90, 145.00) for i in range(20)]
    world = _world(agents)
    ev = Injected_Event(id="e-stable", simTime="2026-03-02T12:00:00+11:00",
                        lat=-37.81, lon=144.95, title="Big Market", scale="city")
    r1 = Event_Propagation().propagate(ev, world)
    r2 = Event_Propagation().propagate(ev, world)
    assert r1.aware_agent_ids == r2.aware_agent_ids
