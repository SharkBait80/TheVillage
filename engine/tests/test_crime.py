"""Tests for the Crime_Engine (Requirement 11)."""
import random
from datetime import datetime

import pytest

from village.clock import localize
from village.crime import (Crime_Engine, CrimeValidationError, LIKELIHOOD_MAX,
                           LIKELIHOOD_MIN, success_likelihood)
from village.models import (Agent, AgentState, CrimeType, LegalStatus, Persona,
                            Outcome, TargetType)


def make_agent(aid, cash=100.0, lat=-37.80, lon=144.96,
               legal=LegalStatus.CLEAR, traits=None):
    state = AgentState(lat=lat, lon=lon, presentLocationId="loc_x",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=cash, legalStatus=legal)
    persona = Persona(name=aid, age=30, occupation="x",
                      traits=traits or ["impulsive"], background="b",
                      homeLocationId="loc_home")
    return Agent(id=aid, persona=persona, state=state)


def sim_time(hour=14):
    return localize(datetime.fromisoformat(f"2026-03-02T{hour:02d}:00:00"))


def test_likelihood_bounds():
    for w in range(0, 20):
        lk = success_likelihood(["impulsive"], w, 14)
        assert LIKELIHOOD_MIN <= lk <= LIKELIHOOD_MAX


def test_likelihood_monotone_non_increasing_in_witnesses():
    prev = None
    for w in range(0, 20):
        lk = success_likelihood(["impulsive", "bold"], w, 2)
        if prev is not None:
            assert lk <= prev + 1e-12
        prev = lk


def test_validate_rejects_charged_or_detained():
    eng = Crime_Engine()
    perp = make_agent("p1", legal=LegalStatus.CHARGED)
    with pytest.raises(CrimeValidationError) as e:
        eng.validate(perp, CrimeType.THEFT, TargetType.AGENT, "v1", (-37.80, 144.96))
    assert e.value.check == "charged_or_detained"


def test_validate_rejects_out_of_range():
    eng = Crime_Engine()
    perp = make_agent("p1", lat=-37.80, lon=144.96)
    # target ~ far away
    with pytest.raises(CrimeValidationError) as e:
        eng.validate(perp, CrimeType.THEFT, TargetType.AGENT, "v1", (-37.90, 145.05))
    assert e.value.check == "target_out_of_range"


def test_validate_accepts_in_range():
    eng = Crime_Engine()
    perp = make_agent("p1", lat=-37.80, lon=144.96)
    eng.validate(perp, CrimeType.THEFT, TargetType.AGENT, "v1", (-37.80001, 144.96001))


def test_theft_transfers_min_of_stolen_and_target_cash():
    # rng.random() always 0 => always succeed
    eng = Crime_Engine(rng=random.Random(0))
    eng._rng = type("R", (), {"random": lambda self: 0.0})()
    perp = make_agent("p1", cash=50.0)
    victim = make_agent("v1", cash=30.0, lat=-37.80, lon=144.96)
    res = eng.resolve(perp, CrimeType.THEFT, TargetType.AGENT, "v1",
                      sim_time(), witnesses=[], stolen_amount=100,
                      target_agent=victim)
    assert res.crime_event.outcome == Outcome.SUCCEEDED
    # transfers min(100, 30) = 30
    assert perp.state.cash == 80.0
    assert victim.state.cash == 0.0


def test_theft_against_zero_cash_target_fails():
    eng = Crime_Engine()
    eng._rng = type("R", (), {"random": lambda self: 0.0})()
    perp = make_agent("p1", cash=50.0)
    victim = make_agent("v1", cash=0.0, lat=-37.80, lon=144.96)
    res = eng.resolve(perp, CrimeType.THEFT, TargetType.AGENT, "v1",
                      sim_time(), witnesses=[], stolen_amount=100,
                      target_agent=victim)
    assert res.crime_event.outcome == Outcome.FAILED
    assert perp.state.cash == 50.0
    assert victim.state.cash == 0.0


def test_location_target_credits_perpetrator():
    eng = Crime_Engine()
    eng._rng = type("R", (), {"random": lambda self: 0.0})()
    perp = make_agent("p1", cash=50.0)
    res = eng.resolve(perp, CrimeType.BURGLARY, TargetType.LOCATION, "loc_shop",
                      sim_time(), witnesses=[], stolen_amount=75)
    assert res.crime_event.outcome == Outcome.SUCCEEDED
    assert perp.state.cash == 125.0


def test_memory_recipients_include_perp_target_witnesses():
    eng = Crime_Engine()
    eng._rng = type("R", (), {"random": lambda self: 0.0})()
    perp = make_agent("p1", cash=50.0)
    victim = make_agent("v1", cash=30.0)
    res = eng.resolve(perp, CrimeType.THEFT, TargetType.AGENT, "v1",
                      sim_time(), witnesses=["w1", "w2"], stolen_amount=10,
                      target_agent=victim)
    assert "p1" in res.memory_recipients
    assert "v1" in res.memory_recipients
    assert "w1" in res.memory_recipients and "w2" in res.memory_recipients


def test_failure_when_roll_exceeds_likelihood():
    eng = Crime_Engine()
    eng._rng = type("R", (), {"random": lambda self: 0.999})()
    perp = make_agent("p1", cash=50.0)
    victim = make_agent("v1", cash=30.0)
    res = eng.resolve(perp, CrimeType.THEFT, TargetType.AGENT, "v1",
                      sim_time(), witnesses=[], stolen_amount=10,
                      target_agent=victim)
    assert res.crime_event.outcome == Outcome.FAILED
    assert perp.state.cash == 50.0
    assert victim.state.cash == 30.0


def test_find_witnesses_within_50m_capped_10():
    eng = Crime_Engine()
    perp = make_agent("p1", lat=-37.80, lon=144.96)
    others = [make_agent(f"w{i}", lat=-37.80, lon=144.96) for i in range(15)]
    witnesses = eng.find_witnesses(perp, others, (-37.80, 144.96))
    assert len(witnesses) == 10
