"""Model invocation budget & throughput (Requirement 18).

Tracks per-sim-hour invocation counts, accumulated USD spend from token
counts * per-1k prices, throttling at the invocation cap, spend-cap pause, and
Bedrock throttling retry with exponential backoff + jitter.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Dict, Optional

from .models import Budget, ModelPrice

INVOCATIONS_MIN = 1
INVOCATIONS_MAX = 100_000
INVOCATIONS_DEFAULT = 5000
SPEND_MIN = 1.00
SPEND_MAX = 10_000.00
SPEND_DEFAULT = 25.00
PRICE_MIN = 0.00
PRICE_MAX = 1000.00

RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_DELAY = 1.0
RETRY_MAX_DELAY = 30.0

VALID_PURPOSES = ("decision_cycle", "conversation", "reflection", "asset_generation")


class BudgetConfigError(ValueError):
    pass


def usd(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def validate_budget(budget: Budget, model_ids_used) -> None:
    """Validate budget config (Req 18.1 / 18.2). Raises BudgetConfigError."""
    inv = budget.maxInvocationsPerSimHour
    if not isinstance(inv, int) or isinstance(inv, bool) or not (INVOCATIONS_MIN <= inv <= INVOCATIONS_MAX):
        raise BudgetConfigError(
            f"maxInvocationsPerSimHour must be an integer {INVOCATIONS_MIN}..{INVOCATIONS_MAX}")
    spend = budget.maxSpendUSD
    if not isinstance(spend, (int, float)) or isinstance(spend, bool) or not (SPEND_MIN <= spend <= SPEND_MAX):
        raise BudgetConfigError(f"maxSpendUSD must be {SPEND_MIN}..{SPEND_MAX}")
    for mid in model_ids_used:
        price = budget.prices.get(mid)
        if price is None:
            raise BudgetConfigError(f"missing price for model '{mid}'")
        for label, val in (("per1kInput", price.per1kInput), ("per1kOutput", price.per1kOutput)):
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not (PRICE_MIN <= val <= PRICE_MAX):
                raise BudgetConfigError(
                    f"price {label} for '{mid}' must be {PRICE_MIN}..{PRICE_MAX}")


@dataclass
class SpendRecord:
    invocations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spend_usd: float = 0.0


class Budget_Accountant:
    """Tracks invocation counts per sim hour and cumulative spend."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self._sim_hour_key: Optional[str] = None
        self._hour_invocations = 0
        self._total_spend = 0.0
        self._paused_for_spend = False
        # per-model/purpose breakdown for cost reports (Req 18.9).
        self.by_model: Dict[str, SpendRecord] = {}
        self.by_purpose: Dict[str, SpendRecord] = {}

    def _hour_key(self, sim_time_iso: str) -> str:
        # 'YYYY-MM-DDTHH' portion identifies the sim hour.
        return sim_time_iso[:13]

    # -- hour reset (Req 18.5) ---------------------------------------------
    def on_tick(self, sim_time_iso: str) -> bool:
        """Advance to the tick's sim hour; reset invocation count on new hour.

        Returns True if a reset happened.
        """
        key = self._hour_key(sim_time_iso)
        if self._sim_hour_key is None:
            self._sim_hour_key = key
            return False
        if key != self._sim_hour_key:
            self._sim_hour_key = key
            self._hour_invocations = 0
            return True
        return False

    # -- throttle gate (Req 18.4) ------------------------------------------
    def can_start_decision(self) -> bool:
        if self._paused_for_spend:
            return False
        return self._hour_invocations < self.budget.maxInvocationsPerSimHour

    def remaining_invocations(self) -> int:
        return max(0, self.budget.maxInvocationsPerSimHour - self._hour_invocations)

    # -- record an invocation (Req 18.3) -----------------------------------
    def record_invocation(self, model_id: str, purpose: str,
                          input_tokens: int, output_tokens: int) -> float:
        """Record an invocation and its spend. Returns incremental USD spend."""
        self._hour_invocations += 1
        price = self.budget.prices.get(model_id)
        if price is None:
            cost = 0.0
        else:
            cost = (input_tokens / 1000.0) * price.per1kInput \
                 + (output_tokens / 1000.0) * price.per1kOutput
        cost = usd(cost)
        self._total_spend = usd(self._total_spend + cost)

        m = self.by_model.setdefault(model_id, SpendRecord())
        m.invocations += 1
        m.input_tokens += input_tokens
        m.output_tokens += output_tokens
        m.spend_usd = usd(m.spend_usd + cost)

        p = self.by_purpose.setdefault(purpose, SpendRecord())
        p.invocations += 1
        p.input_tokens += input_tokens
        p.output_tokens += output_tokens
        p.spend_usd = usd(p.spend_usd + cost)

        if self._total_spend >= self.budget.maxSpendUSD:
            self._paused_for_spend = True
        return cost

    # -- spend cap (Req 18.6) ----------------------------------------------
    @property
    def total_spend(self) -> float:
        return self._total_spend

    @property
    def hour_invocations(self) -> int:
        return self._hour_invocations

    @property
    def paused_for_spend(self) -> bool:
        return self._paused_for_spend

    def spend_cap_reached(self) -> bool:
        return self._total_spend >= self.budget.maxSpendUSD

    # -- cost report (Req 18.9) --------------------------------------------
    def cost_report(self) -> Dict:
        return {
            "totalSpendUSD": usd(self._total_spend),
            "byModel": {k: {
                "invocations": v.invocations,
                "inputTokens": v.input_tokens,
                "outputTokens": v.output_tokens,
                "spendUSD": usd(v.spend_usd),
            } for k, v in self.by_model.items()},
            "byPurpose": {k: {
                "invocations": v.invocations,
                "inputTokens": v.input_tokens,
                "outputTokens": v.output_tokens,
                "spendUSD": usd(v.spend_usd),
            } for k, v in self.by_purpose.items()},
        }


def retry_delays(attempts: int = RETRY_MAX_ATTEMPTS,
                 initial: float = RETRY_INITIAL_DELAY,
                 max_delay: float = RETRY_MAX_DELAY,
                 rng: Optional[random.Random] = None):
    """Yield backoff delays with 0..50% jitter (Req 18.7)."""
    rng = rng or random.Random()
    delay = initial
    for _ in range(attempts):
        jitter = rng.uniform(0.0, 0.5 * delay)
        yield min(max_delay, delay) + jitter
        delay = min(max_delay, delay * 2)


def invoke_with_retry(fn: Callable[[], "object"],
                      is_throttling: Callable[[Exception], bool],
                      sleep: Callable[[float], None],
                      rng: Optional[random.Random] = None):
    """Call fn(), retrying up to 5x on throttling errors (Req 18.7 / 18.8).

    Raises the last exception if all retries fail.
    """
    last_exc: Optional[Exception] = None
    delays = list(retry_delays(rng=rng))
    for attempt in range(RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_throttling(exc) or attempt >= RETRY_MAX_ATTEMPTS:
                raise
            last_exc = exc
            sleep(delays[attempt])
    if last_exc:
        raise last_exc


__all__ = [
    "Budget_Accountant", "SpendRecord", "validate_budget", "BudgetConfigError",
    "retry_delays", "invoke_with_retry", "usd",
    "INVOCATIONS_MIN", "INVOCATIONS_MAX", "INVOCATIONS_DEFAULT",
    "SPEND_MIN", "SPEND_MAX", "SPEND_DEFAULT", "PRICE_MIN", "PRICE_MAX",
    "RETRY_MAX_ATTEMPTS", "VALID_PURPOSES",
]
