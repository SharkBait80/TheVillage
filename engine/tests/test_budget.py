"""Tests for the budget accountant (Requirement 18)."""
import random

import pytest

from village.budget import (Budget_Accountant, BudgetConfigError, invoke_with_retry,
                            retry_delays, validate_budget)
from village.models import Budget, ModelPrice

OPUS = "au.anthropic.claude-opus-5"
HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def make_budget(max_inv=5000, max_spend=25.0):
    return Budget(maxInvocationsPerSimHour=max_inv, maxSpendUSD=max_spend,
                  prices={OPUS: ModelPrice(0.015, 0.075),
                          HAIKU: ModelPrice(0.0008, 0.004)})


def test_validate_ok():
    validate_budget(make_budget(), [OPUS, HAIKU])


def test_validate_rejects_bad_invocations():
    with pytest.raises(BudgetConfigError):
        validate_budget(make_budget(max_inv=0), [OPUS])
    with pytest.raises(BudgetConfigError):
        validate_budget(make_budget(max_inv=200000), [OPUS])


def test_validate_rejects_bad_spend():
    with pytest.raises(BudgetConfigError):
        validate_budget(make_budget(max_spend=0.5), [OPUS])
    with pytest.raises(BudgetConfigError):
        validate_budget(make_budget(max_spend=20000.0), [OPUS])


def test_validate_rejects_missing_price():
    b = Budget(maxInvocationsPerSimHour=5000, maxSpendUSD=25.0,
               prices={OPUS: ModelPrice(0.015, 0.075)})
    with pytest.raises(BudgetConfigError):
        validate_budget(b, [OPUS, HAIKU])  # HAIKU missing


def test_throttle_at_cap():
    acc = Budget_Accountant(make_budget(max_inv=2))
    acc.on_tick("2026-03-02T06:00:00+11:00")
    assert acc.can_start_decision()
    acc.record_invocation(HAIKU, "decision_cycle", 100, 50)
    assert acc.can_start_decision()
    acc.record_invocation(HAIKU, "decision_cycle", 100, 50)
    assert not acc.can_start_decision()  # cap reached


def test_hour_reset_restores_capacity():
    acc = Budget_Accountant(make_budget(max_inv=1))
    acc.on_tick("2026-03-02T06:00:00+11:00")
    acc.record_invocation(HAIKU, "decision_cycle", 10, 10)
    assert not acc.can_start_decision()
    reset = acc.on_tick("2026-03-02T07:00:00+11:00")  # new hour
    assert reset is True
    assert acc.can_start_decision()
    assert acc.hour_invocations == 0


def test_spend_accounting():
    acc = Budget_Accountant(make_budget())
    acc.on_tick("2026-03-02T06:00:00+11:00")
    # 1000 in + 1000 out on opus: 0.015 + 0.075 = 0.09
    acc.record_invocation(OPUS, "decision_cycle", 1000, 1000)
    assert acc.total_spend == 0.09


def test_spend_cap_pauses():
    acc = Budget_Accountant(make_budget(max_spend=1.0))
    acc.on_tick("2026-03-02T06:00:00+11:00")
    # push spend over 1.0
    for _ in range(20):
        acc.record_invocation(OPUS, "decision_cycle", 1000, 1000)
    assert acc.spend_cap_reached()
    assert acc.paused_for_spend
    assert not acc.can_start_decision()


def test_cost_report_breakdown():
    acc = Budget_Accountant(make_budget())
    acc.on_tick("2026-03-02T06:00:00+11:00")
    acc.record_invocation(OPUS, "decision_cycle", 1000, 1000)
    acc.record_invocation(HAIKU, "conversation", 500, 200)
    report = acc.cost_report()
    assert report["byModel"][OPUS]["invocations"] == 1
    assert report["byPurpose"]["conversation"]["invocations"] == 1
    assert report["totalSpendUSD"] > 0


def test_retry_delays_bounded_and_jittered():
    rng = random.Random(42)
    delays = list(retry_delays(rng=rng))
    assert len(delays) == 5
    # each within [base, base*1.5] and <= 30*1.5
    for d in delays:
        assert 0 < d <= 45.0


def test_invoke_with_retry_succeeds_after_throttles():
    calls = {"n": 0}
    slept = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("ThrottlingException")
        return "ok"

    result = invoke_with_retry(
        fn, is_throttling=lambda e: "Throttling" in str(e),
        sleep=lambda d: slept.append(d), rng=random.Random(1))
    assert result == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2


def test_invoke_with_retry_gives_up_after_5():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("ThrottlingException")

    with pytest.raises(RuntimeError):
        invoke_with_retry(fn, is_throttling=lambda e: True,
                          sleep=lambda d: None, rng=random.Random(1))
    assert calls["n"] == 6  # initial + 5 retries
