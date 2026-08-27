"""Tests for the Economy_Engine (Requirement 9)."""
from datetime import datetime

from village.economy import Economy_Engine, money
from village.models import (Agent, AgentState, EmploymentStatus, Job, Location,
                            LocationCategory, OpeningHours, Persona)


def make_agent(cash=100.0, employment=EmploymentStatus.EMPLOYED, job_id="job_1"):
    state = AgentState(lat=-37.8, lon=144.9, presentLocationId="loc_w",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70},
                       cash=cash, employmentStatus=employment, jobId=job_id,
                       dailyLivingCost=40.0)
    persona = Persona(name="Test", age=30, occupation="Barista", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    return Agent(id="agent_1", persona=persona, state=state)


def food_loc(price=12.50):
    return Location(id="loc_f", name="Cafe", category=LocationCategory.FOOD,
                    lat=-37.8, lon=144.9, capacity=100,
                    hours=[OpeningHours("06:00", "22:00")] * 7, price=price)


def test_money_rounds_2dp():
    assert money(12.505) == 12.51
    assert money(12.344) == 12.34


def test_wage_credit_within_shift():
    eng = Economy_Engine()
    a = make_agent(cash=0.0)
    job = Job(id="job_1", locationId="loc_w", occupation="Barista",
              wagePerHour=32.50, shiftStart="07:00", shiftDurationHours=6)
    res = eng.credit_wage(a.state, job, "loc_w", duration_min=120, shift_worked_min=120)
    assert res.earned
    # 2 hours * 32.50 = 65.00
    assert res.new_balance == 65.00


def test_wage_capped_at_shift_duration():
    eng = Economy_Engine()
    a = make_agent(cash=0.0)
    job = Job(id="job_1", locationId="loc_w", occupation="Barista",
              wagePerHour=20.0, shiftStart="07:00", shiftDurationHours=6)
    res = eng.credit_wage(a.state, job, "loc_w", duration_min=600, shift_worked_min=600)
    # capped at 6 hours * 20 = 120
    assert res.new_balance == 120.00


def test_wage_wrong_location_no_credit():
    eng = Economy_Engine()
    a = make_agent(cash=10.0)
    job = Job(id="job_1", locationId="loc_w", occupation="Barista",
              wagePerHour=20.0, shiftStart="07:00", shiftDurationHours=6)
    res = eng.credit_wage(a.state, job, "loc_other", 60, 60)
    assert not res.earned
    assert res.new_balance == 10.0


def test_purchase_debits():
    eng = Economy_Engine()
    a = make_agent(cash=100.0)
    res = eng.purchase(a.state, food_loc(12.50))
    assert res.accepted
    assert res.new_balance == 87.50


def test_purchase_insufficient_funds():
    eng = Economy_Engine()
    a = make_agent(cash=5.0)
    res = eng.purchase(a.state, food_loc(12.50))
    assert not res.accepted
    assert res.reason == "insufficient_funds"
    assert a.state.cash == 5.0


def test_missed_shifts_three_strikes_unemployed():
    eng = Economy_Engine()
    a = make_agent()
    job = Job(id="job_1", locationId="loc_w", occupation="Barista",
              wagePerHour=20.0, shiftStart="07:00", shiftDurationHours=6,
              assignedAgentId="agent_1")
    assert eng.register_shift_attendance(a, job, attended=False) is False
    assert eng.register_shift_attendance(a, job, attended=False) is False
    became = eng.register_shift_attendance(a, job, attended=False)
    assert became is True
    assert a.state.employmentStatus == EmploymentStatus.UNEMPLOYED
    assert a.state.jobId is None
    assert job.assignedAgentId is None


def test_attendance_resets_streak():
    eng = Economy_Engine()
    a = make_agent()
    job = Job(id="job_1", locationId="loc_w", occupation="x",
              wagePerHour=20.0, shiftStart="07:00", shiftDurationHours=6)
    eng.register_shift_attendance(a, job, attended=False)
    eng.register_shift_attendance(a, job, attended=True)
    assert a.state.missedShiftStreak == 0


def test_daily_living_cost_debit():
    eng = Economy_Engine()
    a = make_agent(cash=100.0)
    paid, unpaid = eng.apply_daily_living_cost(a.state)
    assert paid == 40.00
    assert unpaid == 0.0
    assert a.state.cash == 60.00


def test_daily_living_cost_clamps_to_zero():
    eng = Economy_Engine()
    a = make_agent(cash=10.0)
    paid, unpaid = eng.apply_daily_living_cost(a.state)
    assert a.state.cash == 0.00
    assert unpaid == 30.00


def test_financial_pressure():
    eng = Economy_Engine()
    a = make_agent(cash=10.0)  # below 40 living cost
    assert eng.financial_pressure(a.state) == "high"
    a.state.cash = 100.0
    assert eng.financial_pressure(a.state) == "normal"


def test_nearest_open_jobs():
    eng = Economy_Engine()
    a = make_agent(employment=EmploymentStatus.UNEMPLOYED, job_id=None)
    a.state.lat, a.state.lon = -37.80, 144.96
    locs = {
        "l1": Location(id="l1", name="Near", category=LocationCategory.WORKPLACE,
                       lat=-37.801, lon=144.961, capacity=10, hours=[OpeningHours("00:00","23:59")]*7),
        "l2": Location(id="l2", name="Far", category=LocationCategory.WORKPLACE,
                       lat=-37.90, lon=145.05, capacity=10, hours=[OpeningHours("00:00","23:59")]*7),
    }
    jobs = [
        Job(id="j1", locationId="l1", occupation="x", wagePerHour=25.0,
            shiftStart="09:00", shiftDurationHours=8, assignedAgentId=None),
        Job(id="j2", locationId="l2", occupation="y", wagePerHour=30.0,
            shiftStart="09:00", shiftDurationHours=8, assignedAgentId=None),
    ]
    result = eng.nearest_open_jobs(a.state, jobs, locs)
    assert result[0]["locationId"] == "l1"  # nearest first


def test_take_job_assigns_and_employs():
    eng = Economy_Engine()
    a = make_agent(employment=EmploymentStatus.UNEMPLOYED, job_id=None)
    job = Job(id="j1", locationId="l1", occupation="x", wagePerHour=25.0,
              shiftStart="09:00", shiftDurationHours=8, assignedAgentId=None)
    assert eng.take_job(a, job) is True
    assert a.state.employmentStatus == EmploymentStatus.EMPLOYED
    assert a.state.jobId == "j1"
    assert job.assignedAgentId == "agent_1"
