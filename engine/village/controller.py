"""Simulation_Controller — lifecycle state machine (Requirement 2).

start / pause / resume / stop with status running|paused|stopped, restore from
persisted state, forbidden-command rejection, and config validation (Req 4
population, Req 18 budget).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .budget import BudgetConfigError, validate_budget
from .models import Config, SimStatus

POPULATION_MIN = 5
POPULATION_MAX = 2000
# Fallback default when a config omits ``population``. NOTE: the DEPLOYED scale
# is 500 (see seed/config.json and infra sizing, which provisions for 500
# agents); this 25 is only the default for configs that don't specify a count.
POPULATION_DEFAULT = 25


class CommandRejected(Exception):
    def __init__(self, command: str, status: SimStatus, reason: str):
        super().__init__(reason)
        self.command = command
        self.status = status
        self.reason = reason


class ConfigValidationError(ValueError):
    pass


@dataclass
class ControlResult:
    accepted: bool
    status: SimStatus
    message: str = ""


def validate_population(count: Optional[int]) -> int:
    """Validate/normalise a population count (Req 4.2/4.3/4.4)."""
    if count is None:
        return POPULATION_DEFAULT
    if not isinstance(count, int) or isinstance(count, bool) \
            or not (POPULATION_MIN <= count <= POPULATION_MAX):
        raise ConfigValidationError(
            f"population must be an integer {POPULATION_MIN}..{POPULATION_MAX}; got {count!r}")
    return count


def validate_config(config: Config, model_ids_used: Optional[List[str]] = None) -> None:
    """Validate config for start (Req 2 / 4 / 18). Raises on invalid."""
    validate_population(config.population)
    ids = model_ids_used if model_ids_used is not None else list(config.budget.prices.keys())
    try:
        validate_budget(config.budget, ids)
    except BudgetConfigError as e:
        raise ConfigValidationError(str(e)) from e
    if not config.detentionFacilityId:
        raise ConfigValidationError("detentionFacilityId is required")


class Simulation_Controller:
    """Owns the status transitions; the ticker drives the running loop."""

    # allowed transitions: command -> set of statuses from which allowed
    _ALLOWED = {
        "start": {SimStatus.STOPPED},
        "pause": {SimStatus.RUNNING},
        "resume": {SimStatus.PAUSED},
        "stop": {SimStatus.RUNNING, SimStatus.PAUSED},
    }

    def __init__(self, config: Config):
        self.config = config
        self.status = SimStatus(config.status) if isinstance(config.status, str) else config.status

    # -- command validation (Req 2.7) -------------------------------------
    def _forbidden_reason(self, command: str) -> Optional[str]:
        allowed = self._ALLOWED.get(command)
        if allowed is None:
            return f"unknown command '{command}'"
        if self.status not in allowed:
            return (f"command '{command}' forbidden while status is "
                    f"'{self.status.value}'")
        return None

    def can(self, command: str) -> bool:
        return self._forbidden_reason(command) is None

    # -- transitions -------------------------------------------------------
    def start(self, has_persisted_state: bool = False) -> ControlResult:
        reason = self._forbidden_reason("start")
        if reason:
            return ControlResult(False, self.status, reason)
        # config validation before running (Req 2 / 18.2).
        validate_config(self.config)
        self.status = SimStatus.RUNNING
        msg = "restored" if has_persisted_state else "initialised"
        return ControlResult(True, self.status, msg)

    def pause(self) -> ControlResult:
        reason = self._forbidden_reason("pause")
        if reason:
            return ControlResult(False, self.status, reason)
        self.status = SimStatus.PAUSED
        return ControlResult(True, self.status, "paused")

    def resume(self) -> ControlResult:
        reason = self._forbidden_reason("resume")
        if reason:
            return ControlResult(False, self.status, reason)
        self.status = SimStatus.RUNNING
        return ControlResult(True, self.status, "resumed")

    def stop(self) -> ControlResult:
        reason = self._forbidden_reason("stop")
        if reason:
            return ControlResult(False, self.status, reason)
        self.status = SimStatus.STOPPED
        return ControlResult(True, self.status, "stopped")

    def apply(self, command: str, has_persisted_state: bool = False) -> ControlResult:
        """Dispatch a control command string."""
        if command == "start":
            return self.start(has_persisted_state)
        if command == "pause":
            return self.pause()
        if command == "resume":
            return self.resume()
        if command == "stop":
            return self.stop()
        return ControlResult(False, self.status, f"unknown command '{command}'")

    # spend-cap forced pause (Req 18.6) -----------------------------------
    def pause_for_spend(self) -> ControlResult:
        if self.status == SimStatus.RUNNING:
            self.status = SimStatus.PAUSED
            return ControlResult(True, self.status, "paused: spend cap reached")
        return ControlResult(False, self.status, "not running")

    def reject_resume_spend_capped(self) -> ControlResult:
        return ControlResult(False, self.status,
                             "resume rejected: increase maxSpendUSD to continue")


__all__ = [
    "Simulation_Controller", "ControlResult", "CommandRejected",
    "ConfigValidationError", "validate_config", "validate_population",
    "POPULATION_MIN", "POPULATION_MAX", "POPULATION_DEFAULT",
]
