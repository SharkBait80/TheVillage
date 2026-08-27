"""Needs decay & recovery engine (Requirement 5).

Pure functions over an AgentState's needs. Decay is pro-rated per tick
(1 sim minute = 1/60 sim hour); fractional remainders are carried forward in
needsFraction and integer levels are clamped 0..100. Overflow/underflow that
would push a level out of range is discarded (Req 5.5).
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

from .models import ActionType, AgentState, LocationCategory, NEED_NAMES

# Configurable bounds (Req 5.1 / 5.2).
DECAY_RATE_MIN = 0.5
DECAY_RATE_MAX = 20.0
RECOVERY_RATE_MIN = 1.0
RECOVERY_RATE_MAX = 30.0

DEFAULT_DECAY = {"hunger": 6.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
DEFAULT_ENERGY_RECOVERY = 12.0

CRITICAL_THRESHOLD = 20

EAT_RECOVERY = 40         # Req 5.3
SOCIAL_RECOVERY = 15      # Req 5.4
LEISURE_RECOVERY = 25     # Req 5.10
EAT_MIN_MINUTES = 15
LEISURE_MIN_MINUTES = 30
CONVO_MIN_MINUTES = 5

CRITICAL_ENERGY_TRAVEL_MULTIPLIER = 1.5  # Req 5.7


def clamp_rate(rate: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(rate)))


def _clamp_level(v: int) -> int:
    return max(0, min(100, int(v)))


def apply_decay_tick(state: AgentState, decay_rates: Dict[str, float]) -> None:
    """Awake per-tick decay for all four needs (Req 5.1).

    Mutates state.needs / state.needsFraction in place.
    """
    for need in NEED_NAMES:
        rate = clamp_rate(decay_rates.get(need, DEFAULT_DECAY[need]),
                          DECAY_RATE_MIN, DECAY_RATE_MAX)
        per_tick = rate / 60.0
        _apply_delta(state, need, -per_tick)


def apply_energy_recovery_tick(state: AgentState, recovery_rate: float) -> None:
    """Per-tick energy recovery while sleeping (Req 5.2)."""
    rate = clamp_rate(recovery_rate, RECOVERY_RATE_MIN, RECOVERY_RATE_MAX)
    per_tick = rate / 60.0
    _apply_delta(state, "energy", +per_tick)


def _apply_delta(state: AgentState, need: str, delta: float) -> None:
    """Apply a fractional delta with carry, clamping to 0..100.

    Discards any amount that would move the level outside [0,100] (Req 5.5).
    """
    frac = state.needsFraction.get(need, 0.0)
    current = state.needs.get(need, 0)
    total = current + frac + delta
    # Split into integer + fractional remainder.
    new_int = math.floor(total)
    new_frac = total - new_int
    if new_int < 0:
        new_int = 0
        new_frac = 0.0
    elif new_int > 100:
        new_int = 100
        new_frac = 0.0
    state.needs[need] = int(new_int)
    state.needsFraction[need] = float(new_frac)


def apply_instant_recovery(state: AgentState, need: str, amount: int) -> None:
    """Instant integer recovery bump (eat/social/leisure), clamped."""
    state.needs[need] = _clamp_level(state.needs.get(need, 0) + amount)


def on_eat_complete(state: AgentState, duration_min: int,
                    location_category: LocationCategory, is_home: bool) -> bool:
    """Apply +40 hunger for an eat action >=15 min at food/home (Req 5.3)."""
    if duration_min < EAT_MIN_MINUTES:
        return False
    if location_category != LocationCategory.FOOD and not is_home:
        return False
    apply_instant_recovery(state, "hunger", EAT_RECOVERY)
    return True


def on_conversation_complete(state: AgentState, conversation_id: str,
                             duration_min: int) -> bool:
    """Apply +15 social once per conversation >=5 min (Req 5.4)."""
    if duration_min < CONVO_MIN_MINUTES:
        return False
    if conversation_id in state.creditedConversations:
        return False
    apply_instant_recovery(state, "social", SOCIAL_RECOVERY)
    state.creditedConversations.append(conversation_id)
    return True


def on_leisure_complete(state: AgentState, duration_min: int) -> bool:
    """Apply +25 fun for a leisure action >=30 min (Req 5.10)."""
    if duration_min < LEISURE_MIN_MINUTES:
        return False
    apply_instant_recovery(state, "fun", LEISURE_RECOVERY)
    return True


def update_critical_flags(state: AgentState) -> Dict[str, bool]:
    """Mark needs <20 critical, clear at >=20 (Req 5.6 / 5.9). Returns flags."""
    for need in NEED_NAMES:
        state.critical[need] = state.needs.get(need, 0) < CRITICAL_THRESHOLD
    return dict(state.critical)


def critical_needs(state: AgentState) -> Tuple[str, ...]:
    return tuple(n for n in NEED_NAMES if state.critical.get(n, False))


def travel_duration_multiplier(state: AgentState) -> float:
    """1.5x travel multiplier while energy is critical (Req 5.7)."""
    if state.critical.get("energy", False):
        return CRITICAL_ENERGY_TRAVEL_MULTIPLIER
    return 1.0


def apply_critical_energy_travel(duration_min: int, state: AgentState) -> int:
    """Multiply travel duration by 1.5 and round up when energy critical."""
    mult = travel_duration_multiplier(state)
    if mult == 1.0:
        return duration_min
    return int(math.ceil(duration_min * mult))


__all__ = [
    "DECAY_RATE_MIN", "DECAY_RATE_MAX", "RECOVERY_RATE_MIN", "RECOVERY_RATE_MAX",
    "DEFAULT_DECAY", "DEFAULT_ENERGY_RECOVERY", "CRITICAL_THRESHOLD",
    "EAT_RECOVERY", "SOCIAL_RECOVERY", "LEISURE_RECOVERY",
    "clamp_rate", "apply_decay_tick", "apply_energy_recovery_tick",
    "apply_instant_recovery", "on_eat_complete", "on_conversation_complete",
    "on_leisure_complete", "update_critical_flags", "critical_needs",
    "travel_duration_multiplier", "apply_critical_energy_travel",
]
