"""Crime_Engine — validate, resolve, record criminal actions (Requirement 11).

Success likelihood is deterministic, in [0.05, 0.95], and MONOTONE
non-increasing in the witness count with all other inputs held constant
(Req 11.5). Theft/burglary transfers are handled with the Economy engine's
money rounding.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .economy import money
from .models import (Agent, AgentState, CrimeEvent, CrimeType, Detection,
                     LegalStatus, Location, Outcome, TargetType)
from .movement import haversine_m

CRIME_PROXIMITY_M = 50.0        # Req 11.1
LIKELIHOOD_MIN = 0.05           # Req 11.5
LIKELIHOOD_MAX = 0.95
STOLEN_MIN = 1
STOLEN_MAX = 500
MAX_WITNESSES = 10              # Req 11.2


class CrimeValidationError(Exception):
    def __init__(self, check: str):
        super().__init__(check)
        self.check = check


@dataclass
class CrimeResolution:
    crime_event: CrimeEvent
    cash_delta: Dict[str, float]      # agentId/locationId -> signed delta applied
    memory_recipients: List[str]      # perp, target(if agent), witnesses


def success_likelihood(traits: List[str], witness_count: int,
                       hour_of_day: int) -> float:
    """Deterministic success likelihood in [0.05, 0.95].

    MONOTONE non-increasing in witness_count. Traits and hour provide a base
    that is then reduced by a strictly non-negative penalty growing with
    witnesses.
    """
    # Base skill from traits (bounded), deterministic by trait content.
    trait_bonus = 0.0
    boldness = {"impulsive", "bold", "reckless", "cunning", "ruthless", "greedy"}
    caution = {"cautious", "anxious", "timid", "honest", "law-abiding"}
    for t in traits:
        tl = t.strip().lower()
        if tl in boldness:
            trait_bonus += 0.08
        elif tl in caution:
            trait_bonus -= 0.08
    # Hour factor: night (22..05) slightly easier. Bounded, independent of witnesses.
    night = (hour_of_day >= 22 or hour_of_day < 6)
    hour_bonus = 0.10 if night else 0.0

    base = 0.6 + trait_bonus + hour_bonus
    # Witness penalty: monotone non-decreasing in witness_count -> likelihood
    # monotone non-increasing. Saturating so it never overflows.
    witness_penalty = 0.15 * max(0, witness_count)

    raw = base - witness_penalty
    return max(LIKELIHOOD_MIN, min(LIKELIHOOD_MAX, raw))


class Crime_Engine:
    def __init__(self, rng=None):
        # rng.random() in [0,1); injected for determinism. Default: fixed seed.
        if rng is None:
            import random
            rng = random.Random(1337)
        self._rng = rng

    # -- validation (Req 11.1 / 11.6 / 11.8) -------------------------------
    def validate(self, perpetrator: Agent, crime_type: CrimeType,
                 target_type: TargetType, target_id: str,
                 target_position: Tuple[float, float]) -> None:
        """Raise CrimeValidationError on any failed check."""
        if perpetrator.state.legalStatus in (LegalStatus.CHARGED, LegalStatus.DETAINED):
            raise CrimeValidationError("charged_or_detained")
        if not isinstance(crime_type, CrimeType):
            raise CrimeValidationError("invalid_crime_type")
        if not target_id:
            raise CrimeValidationError("missing_target")
        dist = haversine_m(perpetrator.state.lat, perpetrator.state.lon,
                           target_position[0], target_position[1])
        if dist > CRIME_PROXIMITY_M:
            raise CrimeValidationError("target_out_of_range")

    # -- witnesses (Req 11.2) ----------------------------------------------
    def find_witnesses(self, perpetrator: Agent, others: List[Agent],
                       target_position: Tuple[float, float]) -> List[str]:
        near: List[Tuple[float, str]] = []
        for a in others:
            if a.id == perpetrator.id:
                continue
            d = haversine_m(a.state.lat, a.state.lon,
                           target_position[0], target_position[1])
            if d <= CRIME_PROXIMITY_M:
                near.append((d, a.id))
        near.sort(key=lambda t: t[0])
        return [aid for _, aid in near[:MAX_WITNESSES]]

    # -- resolution (Req 11.2-11.5, 11.9, 11.10) ---------------------------
    def resolve(self, perpetrator: Agent, crime_type: CrimeType,
                target_type: TargetType, target_id: str,
                sim_time: datetime, witnesses: List[str],
                stolen_amount: int = 0,
                target_agent: Optional[Agent] = None) -> CrimeResolution:
        """Resolve a validated criminal action into a CrimeEvent + effects."""
        hour = sim_time.hour
        likelihood = success_likelihood(perpetrator.persona.traits,
                                        len(witnesses), hour)
        roll = self._rng.random()
        succeeded = roll < likelihood

        stolen = max(STOLEN_MIN, min(STOLEN_MAX, int(stolen_amount))) if stolen_amount else 0
        cash_delta: Dict[str, float] = {}

        # theft/burglary against a 0-cash agent target -> failed (Req 11.10).
        if succeeded and crime_type in (CrimeType.THEFT, CrimeType.BURGLARY):
            if target_type == TargetType.AGENT:
                if target_agent is None or target_agent.state.cash <= 0.0:
                    succeeded = False
                else:
                    transfer = min(float(stolen), target_agent.state.cash)
                    transfer = money(transfer)
                    target_agent.state.cash = money(target_agent.state.cash - transfer)
                    perpetrator.state.cash = money(perpetrator.state.cash + transfer)
                    cash_delta[target_agent.id] = -transfer
                    cash_delta[perpetrator.id] = transfer
            else:  # location target: credit perpetrator (Req 11.9)
                credit = money(float(stolen))
                perpetrator.state.cash = money(perpetrator.state.cash + credit)
                cash_delta[perpetrator.id] = credit

        outcome = Outcome.SUCCEEDED if succeeded else Outcome.FAILED

        event = CrimeEvent(
            id=str(uuid.uuid4()),
            simTime=sim_time.replace(microsecond=0).isoformat(),
            perpetrator=perpetrator.id,
            crimeType=crime_type,
            targetType=target_type,
            targetId=target_id,
            witnesses=list(witnesses),
            outcome=outcome,
            detection=Detection.UNDETECTED,  # set by Law_Enforcement_Engine
            stolenAmount=stolen,
        )

        # Memory recipients: perp, target(if agent), witnesses (Req 11.4).
        recipients = [perpetrator.id]
        if target_type == TargetType.AGENT:
            recipients.append(target_id)
        recipients.extend(witnesses)

        return CrimeResolution(event, cash_delta, recipients)


__all__ = [
    "Crime_Engine", "CrimeResolution", "CrimeValidationError",
    "success_likelihood", "CRIME_PROXIMITY_M",
    "LIKELIHOOD_MIN", "LIKELIHOOD_MAX", "STOLEN_MIN", "STOLEN_MAX",
]
