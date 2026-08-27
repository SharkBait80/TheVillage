"""Economy_Engine — jobs, wages, purchases, living costs (Requirement 9).

Pure logic operating on Agent/Job/Location models. Cash is AUD, rounded to
2 decimal places. Emits structured results the ticker translates into events.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

from .models import (Agent, AgentState, EmploymentStatus, Job, Location,
                     LocationCategory)
from .movement import haversine_m
from .timeutil import hhmm_minutes

WAGE_MIN = 15.00
WAGE_MAX = 200.00
PRICE_MIN = 0.01
PRICE_MAX = 999.99
LIVING_COST_MIN = 0.00
LIVING_COST_MAX = 999.99
MISSED_SHIFTS_TO_UNEMPLOY = 3    # Req 9.5
WORKPLACE_PROXIMITY_M = 50.0     # Req 9.5
NEAREST_JOBS = 5                 # Req 9.6


def money(x: float) -> float:
    """Round to 2dp with banker-safe half-up."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class WageResult:
    credited: float
    new_balance: float
    earned: bool
    reason: str = ""


@dataclass
class PurchaseResult:
    debited: float
    new_balance: float
    accepted: bool
    reason: str = ""


class Economy_Engine:
    def __init__(self):
        pass

    # -- wages (Req 9.2 / 9.10) --------------------------------------------
    def credit_wage(self, state: AgentState, job: Optional[Job],
                    work_location_id: str, duration_min: int,
                    shift_worked_min: int) -> WageResult:
        """Credit wages for a completed work action inside the shift window.

        `shift_worked_min` = minutes of the action that fell within the shift
        window (capped at shift duration by caller). Returns WageResult.
        """
        if state.employmentStatus != EmploymentStatus.EMPLOYED or job is None:
            return WageResult(0.0, state.cash, False, "not_employed")
        if work_location_id != job.locationId:
            return WageResult(0.0, state.cash, False, "wrong_location")
        capped_min = min(shift_worked_min, job.shiftDurationHours * 60)
        if capped_min <= 0:
            return WageResult(0.0, state.cash, False, "outside_shift")
        hours = capped_min / 60.0
        credit = money(job.wagePerHour * hours)
        state.cash = money(state.cash + credit)
        return WageResult(credit, state.cash, True)

    def shift_overlap_minutes(self, job: Job, action_start: datetime,
                              duration_min: int) -> int:
        """Minutes of [action_start, +duration) that fall in the shift window."""
        start_min = action_start.hour * 60 + action_start.minute
        shift_start = hhmm_minutes(job.shiftStart)
        shift_end = shift_start + job.shiftDurationHours * 60
        action_end = start_min + duration_min
        overlap = min(action_end, shift_end) - max(start_min, shift_start)
        return max(0, overlap)

    # -- purchases (Req 9.3 / 9.4) -----------------------------------------
    def purchase(self, state: AgentState, location: Location) -> PurchaseResult:
        if location.category not in (LocationCategory.FOOD, LocationCategory.RETAIL):
            return PurchaseResult(0.0, state.cash, False, "not_purchasable")
        price = location.price
        if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
            return PurchaseResult(0.0, state.cash, False, "invalid_price")
        price = money(price)
        if price > state.cash:
            return PurchaseResult(0.0, state.cash, False, "insufficient_funds")
        state.cash = money(state.cash - price)
        return PurchaseResult(price, state.cash, True)

    # -- missed shifts (Req 9.5) -------------------------------------------
    def register_shift_attendance(self, agent: Agent, job: Job,
                                   attended: bool) -> bool:
        """Update missed-shift streak; return True if agent became unemployed."""
        st = agent.state
        if attended:
            st.missedShiftStreak = 0
            return False
        st.missedShiftStreak += 1
        if st.missedShiftStreak >= MISSED_SHIFTS_TO_UNEMPLOY:
            st.employmentStatus = EmploymentStatus.UNEMPLOYED
            st.jobId = None
            job.assignedAgentId = None
            st.missedShiftStreak = 0
            return True
        return False

    def attended_shift(self, state: AgentState, job: Job,
                       workplace: Location) -> bool:
        """True if agent within 50m of workplace during first 15 min of shift."""
        return haversine_m(state.lat, state.lon,
                           workplace.lat, workplace.lon) <= WORKPLACE_PROXIMITY_M

    # -- unemployed perception (Req 9.6) -----------------------------------
    def nearest_open_jobs(self, state: AgentState, jobs: List[Job],
                          locations: Dict[str, Location]) -> List[Dict]:
        """5 nearest locations holding an unassigned job (name + wage)."""
        candidates: List[Tuple[float, Dict]] = []
        seen_locs = set()
        for job in jobs:
            if job.assignedAgentId is not None:
                continue
            loc = locations.get(job.locationId)
            if loc is None or job.locationId in seen_locs:
                continue
            seen_locs.add(job.locationId)
            dist = haversine_m(state.lat, state.lon, loc.lat, loc.lon)
            candidates.append((dist, {
                "locationId": loc.id,
                "name": loc.name,
                "wagePerHour": job.wagePerHour,
            }))
        candidates.sort(key=lambda t: t[0])
        return [c[1] for c in candidates[:NEAREST_JOBS]]

    # -- new employment (Req 9.9) ------------------------------------------
    def take_job(self, agent: Agent, job: Job) -> bool:
        """Assign an unassigned job to an unemployed agent doing work there."""
        if agent.state.employmentStatus != EmploymentStatus.UNEMPLOYED:
            return False
        if job.assignedAgentId is not None:
            return False
        job.assignedAgentId = agent.id
        agent.state.jobId = job.id
        agent.state.employmentStatus = EmploymentStatus.EMPLOYED
        return True

    # -- daily living cost (Req 9.7 / 9.11) --------------------------------
    def apply_daily_living_cost(self, state: AgentState) -> Tuple[float, float]:
        """Debit daily living cost on rollover; clamp to 0. Returns (paid, unpaid)."""
        cost = state.dailyLivingCost
        cost = max(LIVING_COST_MIN, min(LIVING_COST_MAX, cost))
        if state.cash >= cost:
            state.cash = money(state.cash - cost)
            return money(cost), 0.0
        unpaid = money(cost - state.cash)
        paid = state.cash
        state.cash = 0.00
        return money(paid), unpaid

    # -- financial pressure (Req 9.8) --------------------------------------
    def financial_pressure(self, state: AgentState) -> str:
        return "high" if state.cash < state.dailyLivingCost else "normal"


__all__ = [
    "Economy_Engine", "WageResult", "PurchaseResult", "money",
    "WAGE_MIN", "WAGE_MAX", "PRICE_MIN", "PRICE_MAX",
    "LIVING_COST_MIN", "LIVING_COST_MAX", "MISSED_SHIFTS_TO_UNEMPLOY", "NEAREST_JOBS",
]
