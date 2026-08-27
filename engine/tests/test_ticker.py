"""Tests for the Ticker loop wiring (DESIGN.md §8) with a fake runtime."""
from datetime import datetime

from village.budget import Budget_Accountant
from village.clock import Simulation_Clock, localize
from village.controller import Simulation_Controller
from village.eventlog import Event_Log
from village.models import (Agent, AgentState, Budget, Config, EmploymentStatus,
                            LegalStatus, Location, LocationCategory, ModelPrice,
                            OpeningHours, Persona, SimStatus)
from village.ticker import Ticker, WorldState

OPUS = "au.anthropic.claude-opus-5"
HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def tick(self, dt):
        self.t += dt


class FakeRuntime:
    """Returns a fixed idle action for every decision."""
    def __init__(self):
        self.decision_calls = 0

    def decision(self, request):
        self.decision_calls += 1
        return {"action": {"type": "idle", "targetType": "location",
                           "targetId": request["state"]["presentLocationId"],
                           "expectedDurationMin": 5}}

    def plan(self, request):
        return {"plan": []}

    def reflect(self, request):
        return {"reflections": []}

    def utterance(self, request):
        return {"utterance": "hi"}


def make_world():
    home = Location(id="loc_home", name="Home", category=LocationCategory.RESIDENCE,
                    lat=-37.81, lon=144.95, capacity=5,
                    hours=[OpeningHours("00:00", "23:59")] * 7)
    config = Config(simId="melb", accelerationFactor=60,
                    detentionFacilityId="loc_remand",
                    budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                          HAIKU: ModelPrice(0.0008, 0.004)}))
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_home",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, employmentStatus=EmploymentStatus.EMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="Aroha", age=34, occupation="Barista",
                      traits=["warm"], background="b", homeLocationId="loc_home")
    agent = Agent(id="agent_01", persona=persona, state=state)
    return WorldState(config=config, agents={"agent_01": agent},
                      locations={"loc_home": home})


def build_ticker():
    world = make_world()
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T06:00:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    runtime = FakeRuntime()
    persisted = []
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=runtime, event_log=log, budget=budget,
                    persist=lambda a: persisted.append(a.id))
    return ticker, fake, runtime, log, persisted


def test_tick_triggers_decision_and_persists():
    ticker, fake, runtime, log, persisted = build_ticker()
    fake.tick(1.0)  # 60x => 1 sim minute => 1 tick
    report = ticker.advance_once()
    assert report is not None
    assert report.tick_count == 1
    # agent had no action -> a decision was triggered
    assert runtime.decision_calls == 1
    assert report.decisions_triggered == 1
    assert "agent_01" in persisted


def test_needs_decay_applied_each_tick():
    ticker, fake, runtime, log, persisted = build_ticker()
    agent = ticker.world.agents["agent_01"]
    start_hunger = agent.state.needs["hunger"]
    # run 60 ticks (1 sim hour) => hunger decays by 6
    for _ in range(60):
        fake.tick(1.0)
        ticker.advance_once()
        # give agent an idle action so it isn't 'sleeping'
    assert agent.state.needs["hunger"] <= start_hunger


def test_paused_controller_no_advance():
    ticker, fake, runtime, log, persisted = build_ticker()
    ticker.controller.status = SimStatus.PAUSED
    fake.tick(10.0)
    report = ticker.advance_once()
    assert report is None
    assert runtime.decision_calls == 0


def test_day_rollover_applies_living_cost():
    world = make_world()
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T23:59:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=FakeRuntime(), event_log=log, budget=budget)
    agent = world.agents["agent_01"]
    cash_before = agent.state.cash
    # cross midnight
    for _ in range(3):
        fake.tick(1.0)
        ticker.advance_once()
    # living cost debited on rollover
    assert agent.state.cash == cash_before - 40.0


# ---------------------------------------------------------------------------
# Thought-process (reasoning + perception) and conversation persistence
# ---------------------------------------------------------------------------

class ReasoningRuntime:
    """Decision returns an action + reasoning; utterance returns canned text."""
    def __init__(self, action_type="idle", reasoning="because I am tired"):
        self.action_type = action_type
        self.reasoning = reasoning
        self.utterance_calls = 0

    def decision(self, request):
        return {
            "action": {"type": self.action_type, "targetType": "location",
                       "targetId": request["state"]["presentLocationId"],
                       "expectedDurationMin": 5},
            "reasoning": self.reasoning,
        }

    def plan(self, request):
        return {"plan": []}

    def reflect(self, request):
        return {"reflections": []}

    def utterance(self, request):
        self.utterance_calls += 1
        who = request.get("agentId", "?")
        return {"utterance": f"hello from {who}"}


def _socialise_action(target_id):
    from village.models import Action, ActionType, TargetType
    return Action(type=ActionType.SOCIALISE, targetType=TargetType.AGENT,
                  targetId=target_id, expectedDurationMin=10,
                  startedSimTime="2026-03-02T06:00:00+11:00")


def test_decision_event_carries_reasoning_and_perception():
    world = make_world()
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T06:00:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    runtime = ReasoningRuntime(reasoning="need to rest")
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=runtime, event_log=log, budget=budget)
    fake.tick(1.0)
    ticker.advance_once()

    action_events = [e for e in log.query(category="action").entries
                     if (e.detail or {}).get("kind") == "accepted"]
    assert action_events, "expected an accepted action event"
    detail = action_events[-1].detail
    assert detail["reasoning"] == "need to rest"
    assert detail["perceptionInput"]["locationId"] == "loc_home"
    assert "needs" in detail["perceptionInput"]
    # decision_trail should surface the reasoning too
    trail = log.decision_trail(action_events[-1].seq)
    assert trail is not None
    assert trail["reasoning"] == "need to rest"


def _two_agent_socialising_world():
    home = Location(id="loc_home", name="Home", category=LocationCategory.RESIDENCE,
                    lat=-37.81, lon=144.95, capacity=5,
                    hours=[OpeningHours("00:00", "23:59")] * 7)
    config = Config(simId="melb", accelerationFactor=60,
                    detentionFacilityId="loc_remand",
                    budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                          HAIKU: ModelPrice(0.0008, 0.004)}))

    def mk(agent_id, name):
        state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_home",
                           needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                           cash=100.0, employmentStatus=EmploymentStatus.EMPLOYED,
                           legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
        persona = Persona(name=name, age=30, occupation="Barista",
                          traits=["warm"], background="b", homeLocationId="loc_home")
        return Agent(id=agent_id, persona=persona, state=state)

    a1 = mk("agent_01", "Aroha")
    a2 = mk("agent_02", "Marco")
    a1.state.currentAction = _socialise_action("agent_02")
    a2.state.currentAction = _socialise_action("agent_01")
    return WorldState(config=config,
                      agents={"agent_01": a1, "agent_02": a2},
                      locations={"loc_home": home})


def test_conversation_is_run_and_persisted_with_utterances():
    world = _two_agent_socialising_world()
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T06:00:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    runtime = ReasoningRuntime()
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=runtime, event_log=log, budget=budget)

    fake.tick(1.0)
    ticker.advance_once()

    convos = log.query(category="conversation").entries
    assert convos, "expected a conversation event to be persisted"
    detail = convos[-1].detail
    assert detail["kind"] == "conversation-ended"
    assert set(detail["participants"]) == {"agent_01", "agent_02"}
    assert len(detail["utterances"]) >= 2
    # utterances carry speaker + text
    first = detail["utterances"][0]
    assert "speaker" in first and "text" in first
    assert runtime.utterance_calls >= 2


# ---------------------------------------------------------------------------
# New: planning + reflection thought logging, and perception enrichment
# ---------------------------------------------------------------------------

class PlanningRuntime:
    """Returns a concrete day-plan, reflections, and travel decisions."""
    def __init__(self):
        self.seen_requests = []

    def decision(self, request):
        self.seen_requests.append(request)
        # choose to travel to the first reachable location if any
        reachable = request.get("reachable") or []
        if reachable:
            return {"action": {"type": "travel", "targetType": "location",
                               "targetId": reachable[0]["id"],
                               "expectedDurationMin": 5},
                    "reasoning": "heading out",
                    "tokenUsage": {"modelId": OPUS, "purpose": "decision_cycle",
                                   "inputTokens": 100, "outputTokens": 20}}
        return {"action": {"type": "idle", "targetType": "location",
                           "targetId": request["state"]["presentLocationId"],
                           "expectedDurationMin": 5}}

    def plan(self, request):
        return {"plan": [{"type": "work", "targetType": "location", "targetId": "loc_home"},
                         {"type": "eat", "targetType": "location", "targetId": "loc_home"},
                         {"type": "sleep", "targetType": "location", "targetId": "loc_home"}],
                "reasoning": "a productive day"}

    def reflect(self, request):
        return {"reflections": [{"text": "I made a new friend today",
                                 "sourceMemoryIds": [1]}]}

    def utterance(self, request):
        return {"utterance": "hi"}


def test_planning_emits_planning_event_and_perception_has_reachable():
    world = make_world()
    # add a second reachable location so travel is possible
    world.locations["loc_cafe"] = Location(
        id="loc_cafe", name="Cafe", category=LocationCategory.FOOD,
        lat=-37.815, lon=144.955, capacity=10,
        hours=[OpeningHours("00:00", "23:59")] * 7, price=8.5)
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T08:00:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    runtime = PlanningRuntime()
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=runtime, event_log=log, budget=budget)
    fake.tick(1.0)
    ticker.advance_once()

    # a planning event was logged (thought process)
    plans = log.query(category="planning").entries
    assert plans, "expected a planning event"
    assert plans[-1].detail["kind"] == "day-plan"
    # perception passed to the decision included reachable locations + colocated
    assert runtime.seen_requests, "expected a decision request"
    req = runtime.seen_requests[-1]
    assert "reachable" in req and len(req["reachable"]) >= 1
    assert "coLocated" in req
    assert "perceptionFlags" in req
    # token usage recorded a model invocation event
    models = log.query(category="model").entries
    assert any(m.detail.get("kind") == "invocation" for m in models)


def test_reflection_emits_memory_events_on_day_rollover():
    world = make_world()
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat("2026-03-02T23:59:00")),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=PlanningRuntime(), event_log=log, budget=budget)
    for _ in range(3):  # cross midnight
        fake.tick(1.0)
        ticker.advance_once()
    reflections = log.query(category="memory").entries
    assert reflections, "expected a reflection (memory) event on rollover"
    assert reflections[-1].detail["kind"] == "reflection"


# ---------------------------------------------------------------------------
# New: behaviour WITHOUT a working LLM runtime (heuristics + local utterances)
# ---------------------------------------------------------------------------

def _multi_location_world():
    """A small world with several categories so heuristics have real options."""
    def L(lid, name, cat, lat, lon, price=None):
        return Location(id=lid, name=name, category=cat, lat=lat, lon=lon,
                        capacity=20, hours=[OpeningHours("00:00", "23:59")] * 7,
                        price=price)
    locs = {
        "loc_home": L("loc_home", "Home", LocationCategory.RESIDENCE, -37.810, 144.950),
        "loc_cafe": L("loc_cafe", "Cafe", LocationCategory.FOOD, -37.812, 144.952, 8.5),
        "loc_park": L("loc_park", "Park", LocationCategory.LEISURE, -37.815, 144.955),
    }
    config = Config(simId="melb", accelerationFactor=60,
                    detentionFacilityId="loc_remand",
                    budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                          HAIKU: ModelPrice(0.0008, 0.004)}))
    state = AgentState(lat=-37.810, lon=144.950, presentLocationId="loc_home",
                       needs={"hunger": 20, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="Aroha", age=34, occupation="Barista",
                      traits=["warm"], background="b", homeLocationId="loc_home")
    agent = Agent(id="agent_01", persona=persona, state=state)
    return WorldState(config=config, agents={"agent_01": agent}, locations=locs)


def _no_runtime_ticker(world, start="2026-03-02T12:00:00"):
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat(start)),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    # runtime=None => harness unavailable; heuristic + local utterances kick in.
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=None, event_log=log, budget=budget)
    return ticker, fake, log


def test_heuristic_decision_when_runtime_none_not_idle():
    world = _multi_location_world()  # hunger low => should seek food
    ticker, fake, log = _no_runtime_ticker(world)
    fake.tick(1.0)
    ticker.advance_once()

    agent = world.agents["agent_01"]
    assert agent.state.currentAction is not None
    # Hungry agent at home should travel to the cafe (not idle).
    act = agent.state.currentAction
    assert act.type.value in ("travel", "eat")
    # A heuristic action event was logged.
    actions = [e for e in log.query(category="action").entries
               if (e.detail or {}).get("kind") == "heuristic"]
    assert actions, "expected a heuristic action event"
    assert actions[-1].detail["action"]["type"] in ("travel", "eat")


def test_no_runtime_conversation_forms_with_local_utterances():
    world = _two_agent_socialising_world()  # both hold socialise->each other
    ticker, fake, log = _no_runtime_ticker(world, start="2026-03-02T12:00:00")
    fake.tick(1.0)
    ticker.advance_once()

    convos = log.query(category="conversation").entries
    assert convos, "expected a conversation even without a runtime"
    detail = convos[-1].detail
    assert detail["kind"] == "conversation-ended"
    assert set(detail["participants"]) == {"agent_01", "agent_02"}
    assert len(detail["utterances"]) >= 2
    # utterances are non-empty strings
    assert all(u["text"].strip() for u in detail["utterances"])


def test_no_runtime_low_social_agents_eventually_converse():
    """With runtime=None, idle low-social co-located agents should choose to
    socialise via the heuristic and form a conversation within a few ticks."""
    def mk(aid, name):
        st = AgentState(lat=-37.815, lon=144.955, presentLocationId="loc_park",
                        needs={"hunger": 70, "energy": 70, "social": 15, "fun": 70},
                        cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                        legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
        persona = Persona(name=name, age=30, occupation="Barista", traits=["warm"],
                          background="b", homeLocationId="loc_home")
        return Agent(id=aid, persona=persona, state=st)

    park = Location(id="loc_park", name="Park", category=LocationCategory.LEISURE,
                    lat=-37.815, lon=144.955, capacity=20,
                    hours=[OpeningHours("00:00", "23:59")] * 7)
    config = Config(simId="melb", accelerationFactor=60,
                    detentionFacilityId="loc_remand",
                    budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                          HAIKU: ModelPrice(0.0008, 0.004)}))
    a1, a2 = mk("agent_01", "Aroha"), mk("agent_02", "Marco")
    world = WorldState(config=config,
                       agents={"agent_01": a1, "agent_02": a2},
                       locations={"loc_park": park})
    ticker, fake, log = _no_runtime_ticker(world)

    formed = False
    for _ in range(5):
        fake.tick(1.0)
        ticker.advance_once()
        if log.query(category="conversation").entries:
            formed = True
            break
    assert formed, "co-located low-social agents should converse via heuristics"


# ---------------------------------------------------------------------------
# New: injected world event propagation into the tick loop
# ---------------------------------------------------------------------------

def _injected(ev_id, **kw):
    from village.events_inject import Injected_Event
    return Injected_Event(id=ev_id, **kw)


def test_injected_severe_event_makes_all_agents_aware_and_emits_system_event():
    world = _two_agent_socialising_world()
    world.injectedEvents["evt-1"] = _injected(
        "evt-1", simTime="2026-03-02T06:00:00+11:00",
        lat=-37.81, lon=144.95, locationId="loc_home",
        title="Explosion", description="a huge blast downtown",
        scale="wide", severity="severe")
    ticker, fake, log = _no_runtime_ticker(world, start="2026-03-02T06:00:00")
    fake.tick(1.0)
    ticker.advance_once()

    sys_events = [e for e in log.query(category="system").entries
                  if (e.detail or {}).get("kind") == "injected-event"]
    assert sys_events, "expected a system event summarising propagation"
    detail = sys_events[-1].detail
    assert detail["awareCount"] == 2
    assert detail["scary"] is True
    # event processed only once
    assert "evt-1" in ticker.world.processedEventIds
    # scary event flagged the location for avoidance on aware agents
    for aid in ("agent_01", "agent_02"):
        assert "loc_home" in world.agents[aid].state.avoidedLocations


def test_injected_event_processed_only_once():
    world = _two_agent_socialising_world()
    world.injectedEvents["evt-1"] = _injected(
        "evt-1", simTime="2026-03-02T06:00:00+11:00",
        lat=-37.81, lon=144.95, title="City Alert", scale="wide", severity="major")
    ticker, fake, log = _no_runtime_ticker(world, start="2026-03-02T06:00:00")
    for _ in range(3):
        fake.tick(1.0)
        ticker.advance_once()
    sys_events = [e for e in log.query(category="system").entries
                  if (e.detail or {}).get("kind") == "injected-event"]
    assert len(sys_events) == 1, "event should propagate exactly once"


def test_injected_event_memory_referenced_in_conversation():
    world = _two_agent_socialising_world()
    world.injectedEvents["evt-1"] = _injected(
        "evt-1", simTime="2026-03-02T06:00:00+11:00",
        lat=-37.81, lon=144.95, locationId="loc_home",
        title="Explosion", description="a blast rattled the windows",
        scale="wide", severity="severe")
    ticker, fake, log = _no_runtime_ticker(world, start="2026-03-02T06:00:00")
    # A few ticks so awareness is recorded and conversations run.
    saw_event_talk = False
    for _ in range(4):
        fake.tick(1.0)
        ticker.advance_once()
        for e in log.query(category="conversation").entries:
            for u in (e.detail or {}).get("utterances", []):
                if "Explosion" in u["text"]:
                    saw_event_talk = True
    assert saw_event_talk, "agents should talk about the injected explosion"


def test_injected_event_future_time_not_processed_early():
    world = _two_agent_socialising_world()
    world.injectedEvents["evt-future"] = _injected(
        "evt-future", simTime="2026-03-02T09:00:00+11:00",
        lat=-37.81, lon=144.95, title="Later Festival", scale="wide")
    ticker, fake, log = _no_runtime_ticker(world, start="2026-03-02T06:00:00")
    fake.tick(1.0)
    ticker.advance_once()
    sys_events = [e for e in log.query(category="system").entries
                  if (e.detail or {}).get("kind") == "injected-event"]
    assert not sys_events, "future-dated event must not fire yet"
    assert "evt-future" not in ticker.world.processedEventIds


def test_agentstate_new_fields_roundtrip():
    """avoidedLocations + attractorLocation persist through to_dict/from_dict."""
    st = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_home",
                    needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70})
    st.avoidedLocations = {"loc_x": "2026-03-02T18:00:00+11:00"}
    st.attractorLocation = {"locationId": "loc_fest",
                            "expiry": "2026-03-02T20:00:00+11:00"}
    again = AgentState.from_dict(st.to_dict())
    assert again.avoidedLocations == {"loc_x": "2026-03-02T18:00:00+11:00"}
    assert again.attractorLocation == {"locationId": "loc_fest",
                                       "expiry": "2026-03-02T20:00:00+11:00"}


def test_agentstate_defaults_backward_compatible():
    """Old persisted items lacking the new fields still parse (empty defaults)."""
    old = {
        "lat": -37.81, "lon": 144.95, "presentLocationId": "loc_home",
        "needs": {"hunger": 70, "energy": 70, "social": 70, "fun": 70},
    }
    st = AgentState.from_dict(old)
    assert st.avoidedLocations == {}
    assert st.attractorLocation is None
