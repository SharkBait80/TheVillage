"""Tests for action-completion side-effects wired into the tick loop.

These verify that finished actions actually apply their real economic / need /
crime consequences through ``Ticker.advance_once`` (not just in the standalone
engines). Regression guard for the integration gap where ``_progress_action``
cleared actions with no side effects.
"""
from datetime import datetime

from village.budget import Budget_Accountant
from village.clock import Simulation_Clock, localize
from village.controller import Simulation_Controller
from village.crime import Crime_Engine
from village.eventlog import Event_Log
from village.law import Law_Enforcement_Engine
from village.models import (Action, ActionType, Agent, AgentState, Budget,
                            Config, CrimeType, EmploymentStatus, Job,
                            LegalStatus, Location, LocationCategory, ModelPrice,
                            OpeningHours, Persona, TargetType)
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


class NoRuntime:
    """Runtime that never assigns a new action (returns bare idle).

    Combined with a pre-set currentAction this lets us isolate completion
    effects: the pre-set action runs to completion, and the follow-up idle
    decision does not create economic effects.
    """

    def decision(self, request):
        return {"action": {"type": "idle", "targetType": "location",
                           "targetId": request["state"]["presentLocationId"],
                           "expectedDurationMin": 5}}

    def plan(self, request):
        return {"plan": []}

    def reflect(self, request):
        return {"reflections": []}

    def utterance(self, request):
        return {"utterance": "hi"}


def _hours():
    return [OpeningHours("00:00", "23:59")] * 7


def _config():
    return Config(simId="melb", accelerationFactor=60,
                  detentionFacilityId="loc_remand",
                  budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                        HAIKU: ModelPrice(0.0008, 0.004)}))


def _ticker(world, start="2026-03-02T09:00:00", law=None):
    fake = FakeClock()
    clock = Simulation_Clock(
        localize(datetime.fromisoformat(start)),
        acceleration_factor=60, real_clock=fake)
    controller = Simulation_Controller(world.config)
    controller.start()
    budget = Budget_Accountant(world.config.budget)
    log = Event_Log()
    ticker = Ticker(world=world, clock=clock, controller=controller,
                    runtime=NoRuntime(), event_log=log, budget=budget, law=law)
    return ticker, fake, log


def _run_ticks(ticker, fake, n=1):
    for _ in range(n):
        fake.tick(1.0)
        ticker.advance_once()


# ---------------------------------------------------------------------------
# Wages (Req 9.2 / 9.10)
# ---------------------------------------------------------------------------
def test_completed_work_credits_wages():
    work = Location(id="loc_cafe", name="Cafe", category=LocationCategory.FOOD,
                    lat=-37.81, lon=144.95, capacity=10, hours=_hours(),
                    price=5.0)
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_cafe",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, employmentStatus=EmploymentStatus.EMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0,
                       jobId="job_1")
    persona = Persona(name="Aroha", age=34, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId="loc_cafe")
    agent = Agent(id="agent_01", persona=persona, state=state)
    job = Job(id="job_1", locationId="loc_cafe", occupation="Barista",
              wagePerHour=30.0, shiftStart="09:00", shiftDurationHours=8,
              assignedAgentId="agent_01")
    world = WorldState(config=_config(), agents={"agent_01": agent},
                       locations={"loc_cafe": work}, jobs={"job_1": job})
    # 60-minute work action, one minute from completing.
    agent.state.currentAction = Action(
        type=ActionType.WORK, targetType=TargetType.LOCATION,
        targetId="loc_cafe", expectedDurationMin=60, progress=59.0 / 60.0,
        startedSimTime="2026-03-02T09:00:00+11:00")
    ticker, fake, log = _ticker(world)
    _run_ticks(ticker, fake, 1)
    # 60 min at 30/hr = 30.00 credited.
    assert agent.state.cash == 130.0
    wage_events = [e for e in log.query(category="employment").entries
                   if (e.detail or {}).get("kind") == "wage-credited"]
    assert wage_events, "expected a wage-credited event"


# ---------------------------------------------------------------------------
# Purchases (Req 9.3 / 9.4)
# ---------------------------------------------------------------------------
def test_completed_shop_debits_cash():
    shop = Location(id="loc_shop", name="Shop", category=LocationCategory.RETAIL,
                    lat=-37.81, lon=144.95, capacity=10, hours=_hours(),
                    price=25.0)
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_shop",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="Bo", age=40, occupation="Shopper", traits=["thrifty"],
                      background="b", homeLocationId="loc_shop")
    agent = Agent(id="agent_02", persona=persona, state=state)
    world = WorldState(config=_config(), agents={"agent_02": agent},
                       locations={"loc_shop": shop})
    agent.state.currentAction = Action(
        type=ActionType.SHOP, targetType=TargetType.LOCATION,
        targetId="loc_shop", expectedDurationMin=1, progress=0.0,
        startedSimTime="2026-03-02T09:00:00+11:00")
    ticker, fake, log = _ticker(world)
    _run_ticks(ticker, fake, 1)
    assert agent.state.cash == 75.0
    buys = [e for e in log.query(category="economy").entries
            if (e.detail or {}).get("kind") == "purchase"]
    assert buys


# ---------------------------------------------------------------------------
# Need recovery on completion (Req 5.3 / 5.10)
# ---------------------------------------------------------------------------
def test_completed_eat_recovers_hunger():
    cafe = Location(id="loc_cafe", name="Cafe", category=LocationCategory.FOOD,
                    lat=-37.81, lon=144.95, capacity=10, hours=_hours(), price=5.0)
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_cafe",
                       needs={"hunger": 30, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="Cy", age=22, occupation="Student", traits=["hungry"],
                      background="b", homeLocationId="loc_cafe")
    agent = Agent(id="agent_03", persona=persona, state=state)
    world = WorldState(config=_config(), agents={"agent_03": agent},
                       locations={"loc_cafe": cafe})
    agent.state.currentAction = Action(
        type=ActionType.EAT, targetType=TargetType.LOCATION,
        targetId="loc_cafe", expectedDurationMin=20, progress=19.0 / 20.0,
        startedSimTime="2026-03-02T09:00:00+11:00")
    ticker, fake, log = _ticker(world)
    _run_ticks(ticker, fake, 1)
    # +40 hunger (minus at most ~1 point of decay this tick).
    assert agent.state.needs["hunger"] >= 68


def test_completed_leisure_recovers_fun():
    park = Location(id="loc_park", name="Park", category=LocationCategory.LEISURE,
                    lat=-37.81, lon=144.95, capacity=50, hours=_hours())
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_park",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 30},
                       cash=100.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="Di", age=51, occupation="Retiree", traits=["playful"],
                      background="b", homeLocationId="loc_park")
    agent = Agent(id="agent_04", persona=persona, state=state)
    world = WorldState(config=_config(), agents={"agent_04": agent},
                       locations={"loc_park": park})
    agent.state.currentAction = Action(
        type=ActionType.LEISURE, targetType=TargetType.LOCATION,
        targetId="loc_park", expectedDurationMin=40, progress=39.0 / 40.0,
        startedSimTime="2026-03-02T09:00:00+11:00")
    ticker, fake, log = _ticker(world)
    _run_ticks(ticker, fake, 1)
    assert agent.state.needs["fun"] >= 53  # +25 minus a little decay


# ---------------------------------------------------------------------------
# Unemployed agent takes an open job by working there (Req 9.9)
# ---------------------------------------------------------------------------
def test_unemployed_takes_open_job_on_work_completion():
    work = Location(id="loc_cafe", name="Cafe", category=LocationCategory.FOOD,
                    lat=-37.81, lon=144.95, capacity=10, hours=_hours(), price=5.0)
    state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_cafe",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=50.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                       legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    persona = Persona(name="El", age=29, occupation="Seeker", traits=["eager"],
                      background="b", homeLocationId="loc_cafe")
    agent = Agent(id="agent_05", persona=persona, state=state)
    job = Job(id="job_open", locationId="loc_cafe", occupation="Barista",
              wagePerHour=25.0, shiftStart="09:00", shiftDurationHours=8,
              assignedAgentId=None)
    world = WorldState(config=_config(), agents={"agent_05": agent},
                       locations={"loc_cafe": work}, jobs={"job_open": job})
    agent.state.currentAction = Action(
        type=ActionType.WORK, targetType=TargetType.LOCATION,
        targetId="loc_cafe", expectedDurationMin=60, progress=59.0 / 60.0,
        startedSimTime="2026-03-02T09:00:00+11:00")
    ticker, fake, log = _ticker(world)
    _run_ticks(ticker, fake, 1)
    assert agent.state.employmentStatus == EmploymentStatus.EMPLOYED
    assert agent.state.jobId == "job_open"
    assert job.assignedAgentId == "agent_05"
    # and got paid for the shift just worked
    assert agent.state.cash > 50.0


# ---------------------------------------------------------------------------
# Crime resolution + law enforcement (Req 11 / 12)
# ---------------------------------------------------------------------------
def test_completed_crime_resolves_and_law_enforces():
    street = Location(id="loc_street", name="Street", category=LocationCategory.CIVIC,
                      lat=-37.81, lon=144.95, capacity=100, hours=_hours())
    remand = Location(id="loc_remand", name="Remand", category=LocationCategory.CIVIC,
                      lat=-37.90, lon=144.90, capacity=100, hours=_hours())
    perp_state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_street",
                            needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                            cash=10.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                            legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    perp = Agent(id="perp", persona=Persona(
        name="Vic", age=33, occupation="Rogue", traits=["bold", "reckless"],
        background="b", homeLocationId="loc_street"), state=perp_state)
    victim_state = AgentState(lat=-37.81, lon=144.95, presentLocationId="loc_street",
                              needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                              cash=200.0, employmentStatus=EmploymentStatus.UNEMPLOYED,
                              legalStatus=LegalStatus.CLEAR, dailyLivingCost=40.0)
    victim = Agent(id="victim", persona=Persona(
        name="Wen", age=45, occupation="Clerk", traits=["calm"],
        background="b", homeLocationId="loc_street"), state=victim_state)
    world = WorldState(config=_config(), agents={"perp": perp, "victim": victim},
                       locations={"loc_street": street, "loc_remand": remand})
    # Witnesses have negative sentiment => detected.
    law = Law_Enforcement_Engine(detention_facility=remand,
                                 sentiment_lookup=lambda w, p: -1)
    # Deterministic RNG that always succeeds (roll 0.0 < likelihood).
    import random
    ticker, fake, log = _ticker(world, law=law)
    ticker.crime = Crime_Engine(rng=random.Random(0))
    perp.state.currentAction = Action(
        type=ActionType.COMMIT_CRIME, targetType=TargetType.AGENT,
        targetId="victim", expectedDurationMin=1, progress=0.0,
        crimeType=CrimeType.THEFT,
        startedSimTime="2026-03-02T09:00:00+11:00")
    _run_ticks(ticker, fake, 1)
    crime_events = [e for e in log.query(category="crime").entries
                    if (e.detail or {}).get("kind") == "crime-resolved"]
    assert crime_events, "expected a resolved crime event"
    # Detected crime => perpetrator at least suspected.
    assert perp.state.legalStatus in (LegalStatus.SUSPECTED, LegalStatus.CHARGED,
                                      LegalStatus.DETAINED)
    legal_events = log.query(category="legal").entries
    assert legal_events, "expected a law-enforcement event"
