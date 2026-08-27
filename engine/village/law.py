"""Law_Enforcement_Engine — detection & consequences (Requirement 12).

Deterministic status machine: clear -> suspected -> charged -> detained ->
(release) clear. Detection score counts witnesses with sentiment<=0 toward
the perpetrator. Auto-clear of suspected after 7 sim days with no new detected
crime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .clock import localize
from .models import (Agent, AgentState, CrimeEvent, Detection, EmploymentStatus,
                     LegalStatus, Location)

DETENTION_HOURS_PER_CRIME = 24
DETENTION_MIN_HOURS = 12
DETENTION_MAX_HOURS = 72
CHARGE_THRESHOLD_FURTHER = 2      # 2 further detected after suspected (Req 12.4)
SUSPECT_AUTOCLEAR_DAYS = 7        # Req 12.11
DETAINED_ALLOWED_ACTIONS = ("sleep", "eat", "socialise", "idle")


@dataclass
class LawOutcome:
    detection: Detection
    detection_score: int
    new_status: LegalStatus
    status_changed: bool
    detained: bool = False
    released: bool = False
    release_sim_time: Optional[str] = None
    events: List[str] = None  # human-readable event descriptions

    def __post_init__(self):
        if self.events is None:
            self.events = []


class Law_Enforcement_Engine:
    def __init__(self, detention_facility: Location,
                 sentiment_lookup: Callable[[str, str], int]):
        """`sentiment_lookup(witness_id, perp_id)` -> sentiment int."""
        self.detention_facility = detention_facility
        self._sentiment = sentiment_lookup

    # -- detection (Req 12.1) ----------------------------------------------
    def compute_detection(self, crime: CrimeEvent) -> Tuple[Detection, int]:
        score = 0
        for w in crime.witnesses:
            if self._sentiment(w, crime.perpetrator) <= 0:
                score += 1
        detection = Detection.DETECTED if score >= 1 else Detection.UNDETECTED
        return detection, score

    # -- process a resolved crime (Req 12.1-12.5, 12.8) --------------------
    def process_crime(self, perpetrator: Agent, crime: CrimeEvent,
                      sim_time: datetime) -> LawOutcome:
        detection, score = self.compute_detection(crime)
        crime.detection = detection
        st = perpetrator.state
        outcome = LawOutcome(detection=detection, detection_score=score,
                             new_status=st.legalStatus, status_changed=False)

        if detection != Detection.DETECTED:
            return outcome

        st.detectedCrimeCount += 1

        if st.legalStatus == LegalStatus.CLEAR:
            st.legalStatus = LegalStatus.SUSPECTED
            st.suspectedSince = sim_time.replace(microsecond=0).isoformat()
            outcome.new_status = LegalStatus.SUSPECTED
            outcome.status_changed = True
            outcome.events.append(
                f"detection: {perpetrator.id} now suspected (score={score})")
            return outcome

        if st.legalStatus == LegalStatus.SUSPECTED:
            # Charged when 2 further detected crimes after the suspecting event.
            # detectedCrimeCount == 1 set the suspected status; +2 => count>=3.
            if st.detectedCrimeCount >= 1 + CHARGE_THRESHOLD_FURTHER:
                self._charge_and_detain(perpetrator, sim_time, outcome)
            else:
                outcome.events.append(
                    f"detection: {perpetrator.id} further detected "
                    f"(count={st.detectedCrimeCount})")
        return outcome

    def _charge_and_detain(self, agent: Agent, sim_time: datetime,
                           outcome: LawOutcome) -> None:
        st = agent.state
        st.legalStatus = LegalStatus.CHARGED
        outcome.events.append(f"legal: {agent.id} charged")
        # Immediately detain (Req 12.5).
        st.legalStatus = LegalStatus.DETAINED
        st.lat = self.detention_facility.lat
        st.lon = self.detention_facility.lon
        st.presentLocationId = self.detention_facility.id
        hours = DETENTION_HOURS_PER_CRIME * st.detectedCrimeCount
        hours = max(DETENTION_MIN_HOURS, min(DETENTION_MAX_HOURS, hours))
        release = localize(sim_time) + timedelta(hours=hours)
        st.detainedReleaseSimTime = release.replace(microsecond=0).isoformat()
        # Employment suspended, job retained (Req 12.8).
        if st.employmentStatus == EmploymentStatus.EMPLOYED:
            st.employmentStatus = EmploymentStatus.SUSPENDED
        outcome.new_status = LegalStatus.DETAINED
        outcome.status_changed = True
        outcome.detained = True
        outcome.release_sim_time = st.detainedReleaseSimTime
        outcome.events.append(
            f"legal: {agent.id} detained until {st.detainedReleaseSimTime}")

    # -- release (Req 12.7 / 12.10) ----------------------------------------
    def check_release(self, agent: Agent, sim_time: datetime,
                      home_location: Location, job_exists: bool) -> Optional[LawOutcome]:
        st = agent.state
        if st.legalStatus != LegalStatus.DETAINED or not st.detainedReleaseSimTime:
            return None
        release_dt = datetime.fromisoformat(st.detainedReleaseSimTime)
        if localize(sim_time) < release_dt:
            return None
        st.legalStatus = LegalStatus.CLEAR
        st.lat = home_location.lat
        st.lon = home_location.lon
        st.presentLocationId = home_location.id
        st.detectedCrimeCount = 0
        st.detainedReleaseSimTime = None
        st.suspectedSince = None
        # Restore employment (Req 12.10).
        st.employmentStatus = (EmploymentStatus.EMPLOYED if job_exists
                               else EmploymentStatus.UNEMPLOYED)
        outcome = LawOutcome(detection=Detection.UNDETECTED, detection_score=0,
                             new_status=LegalStatus.CLEAR, status_changed=True,
                             released=True)
        outcome.events.append(f"legal: {agent.id} released -> clear")
        return outcome

    # -- suspect auto-clear (Req 12.11) ------------------------------------
    def check_suspect_autoclear(self, agent: Agent,
                                sim_time: datetime) -> Optional[LawOutcome]:
        st = agent.state
        if st.legalStatus != LegalStatus.SUSPECTED or not st.suspectedSince:
            return None
        since = datetime.fromisoformat(st.suspectedSince)
        if localize(sim_time) - since < timedelta(days=SUSPECT_AUTOCLEAR_DAYS):
            return None
        st.legalStatus = LegalStatus.CLEAR
        st.suspectedSince = None
        outcome = LawOutcome(detection=Detection.UNDETECTED, detection_score=0,
                             new_status=LegalStatus.CLEAR, status_changed=True)
        outcome.events.append(f"legal: {agent.id} suspicion auto-cleared")
        return outcome

    # -- detained action restriction (Req 12.6 / 12.9) --------------------
    def validate_detained_action(self, action_type: str,
                                 target_id: str) -> bool:
        return (action_type in DETAINED_ALLOWED_ACTIONS
                and target_id == self.detention_facility.id)


__all__ = [
    "Law_Enforcement_Engine", "LawOutcome",
    "DETENTION_HOURS_PER_CRIME", "DETENTION_MIN_HOURS", "DETENTION_MAX_HOURS",
    "CHARGE_THRESHOLD_FURTHER", "SUSPECT_AUTOCLEAR_DAYS", "DETAINED_ALLOWED_ACTIONS",
]
