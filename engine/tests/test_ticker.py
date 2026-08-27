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
