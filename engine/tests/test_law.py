"""Tests for the Law_Enforcement_Engine (Requirement 12)."""
from datetime import datetime, timedelta

from village.clock import localize
from village.law import Law_Enforcement_Engine
from village.models import (Agent, AgentState, CrimeEvent, CrimeType, Detection,
                            EmploymentStatus, LegalStatus, Location,
                            LocationCategory, OpeningHours, Outcome, Persona,
                            TargetType)


def detention_facility():
    return Location(id="loc_remand", name="Remand", category=LocationCategory.CIVIC,
                    lat=-37.79, lon=144.99, capacity=1000,
                    hours=[OpeningHours("00:00", "23:59")] * 7,
                    isDetentionFacility=True)


def home_loc():
    return Location(id="loc_home", name="Home", category=LocationCategory.RESIDENCE,
                    lat=-37.81, lon=144.95, capacity=5,
                    hours=[OpeningHours("00:00", "23:59")] * 7)


def make_agent(aid="p1", legal=LegalStatus.CLEAR,
               employment=EmploymentStatus.EMPLOYED):
    state = AgentState(lat=-37.80, lon=144.96, presentLocationId="loc_x",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=100.0, legalStatus=legal, employmentStatus=employment,
                       jobId="job_1")
    persona = Persona(name=aid, age=30, occupation="x", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    return Agent(id=aid, persona=persona, state=state)


def crime(perp="p1", witnesses=None):
    return CrimeEvent(id="c1", simTime="2026-03-02T14:00:00+11:00",
                      perpetrator=perp, crimeType=CrimeType.THEFT,
                      targetType=TargetType.AGENT, targetId="v1",
                      witnesses=witnesses or [], outcome=Outcome.SUCCEEDED)


def st(hour=14, day=2):
    return localize(datetime.fromisoformat(f"2026-03-{day:02d}T{hour:02d}:00:00"))


def make_law(sentiments=None):
    sentiments = sentiments or {}
    return Law_Enforcement_Engine(
        detention_facility(),
        sentiment_lookup=lambda w, p: sentiments.get((w, p), 0))


def test_detection_counts_nonpositive_sentiment_witnesses():
    law = make_law({("w1", "p1"): 0, ("w2", "p1"): -5, ("w3", "p1"): 10})
    c = crime(witnesses=["w1", "w2", "w3"])
    detection, score = law.compute_detection(c)
    assert detection == Detection.DETECTED
    assert score == 2  # w1(0) and w2(-5), not w3(10)


def test_undetected_when_no_hostile_witnesses():
    law = make_law({("w1", "p1"): 5})
    c = crime(witnesses=["w1"])
    detection, score = law.compute_detection(c)
    assert detection == Detection.UNDETECTED
    assert score == 0


def test_clear_to_suspected_on_first_detected():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    out = law.process_crime(a, crime(witnesses=["w1"]), st())
    assert a.state.legalStatus == LegalStatus.SUSPECTED
    assert a.state.detectedCrimeCount == 1
    assert out.status_changed


def test_two_further_detected_charge_and_detain():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    law.process_crime(a, crime(witnesses=["w1"]), st())     # suspected, count 1
    law.process_crime(a, crime(witnesses=["w1"]), st())     # count 2
    out = law.process_crime(a, crime(witnesses=["w1"]), st())  # count 3 -> charged/detained
    assert a.state.legalStatus == LegalStatus.DETAINED
    assert a.state.presentLocationId == "loc_remand"
    assert a.state.employmentStatus == EmploymentStatus.SUSPENDED
    assert a.state.detainedReleaseSimTime is not None
    assert out.detained


def test_detention_duration_clamped():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    # push to detained with 3 detected crimes -> 24*3=72h (at max)
    for _ in range(3):
        law.process_crime(a, crime(witnesses=["w1"]), st())
    release = datetime.fromisoformat(a.state.detainedReleaseSimTime)
    detained_at = st()
    delta_h = (release - detained_at).total_seconds() / 3600
    assert 12 <= delta_h <= 72
    assert abs(delta_h - 72) < 1e-6


def test_release_resets_and_restores_employment():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    for _ in range(3):
        law.process_crime(a, crime(witnesses=["w1"]), st())
    release_dt = datetime.fromisoformat(a.state.detainedReleaseSimTime)
    # before release -> None
    assert law.check_release(a, release_dt - timedelta(minutes=1), home_loc(), True) is None
    # at/after release
    out = law.check_release(a, release_dt, home_loc(), job_exists=True)
    assert out is not None
    assert a.state.legalStatus == LegalStatus.CLEAR
    assert a.state.presentLocationId == "loc_home"
    assert a.state.detectedCrimeCount == 0
    assert a.state.employmentStatus == EmploymentStatus.EMPLOYED


def test_release_unemployed_when_job_gone():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    for _ in range(3):
        law.process_crime(a, crime(witnesses=["w1"]), st())
    release_dt = datetime.fromisoformat(a.state.detainedReleaseSimTime)
    law.check_release(a, release_dt, home_loc(), job_exists=False)
    assert a.state.employmentStatus == EmploymentStatus.UNEMPLOYED


def test_suspect_autoclears_after_7_days():
    law = make_law({("w1", "p1"): 0})
    a = make_agent()
    law.process_crime(a, crime(witnesses=["w1"]), st(day=2))
    assert a.state.legalStatus == LegalStatus.SUSPECTED
    # before 7 days -> no clear
    assert law.check_suspect_autoclear(a, st(day=8)) is None
    # after 7 days
    out = law.check_suspect_autoclear(a, st(day=9))
    assert out is not None
    assert a.state.legalStatus == LegalStatus.CLEAR


def test_detained_action_restriction():
    law = make_law()
    assert law.validate_detained_action("sleep", "loc_remand") is True
    assert law.validate_detained_action("eat", "loc_remand") is True
    assert law.validate_detained_action("work", "loc_remand") is False
    assert law.validate_detained_action("idle", "loc_other") is False
