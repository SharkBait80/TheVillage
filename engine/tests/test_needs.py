"""Tests for the Needs engine (Requirement 5)."""
from village.models import AgentState, EmploymentStatus, LegalStatus, LocationCategory
from village.needs import (CRITICAL_THRESHOLD, apply_critical_energy_travel,
                           apply_decay_tick, apply_energy_recovery_tick,
                           clamp_rate, on_conversation_complete, on_eat_complete,
                           on_leisure_complete, update_critical_flags)


def make_state(**needs):
    base = {"hunger": 70, "energy": 70, "social": 70, "fun": 70}
    base.update(needs)
    return AgentState(lat=-37.8, lon=144.9, presentLocationId="loc_home", needs=base)


def test_decay_prorated_and_carries_fraction():
    st = make_state()
    rates = {"hunger": 6.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
    # 6/hr hunger => 0.1 per tick. After 1 tick, level stays 69 (70-0.1 floored)
    apply_decay_tick(st, rates)
    assert st.needs["hunger"] == 69
    assert abs(st.needsFraction["hunger"] - 0.9) < 1e-9
    # after 10 ticks total, hunger dropped a full point (0.1*10 = 1.0)
    for _ in range(9):
        apply_decay_tick(st, rates)
    assert st.needs["hunger"] == 69  # 70 - 1.0 = 69 exactly


def test_decay_full_hour_equals_rate():
    st = make_state(hunger=100)
    rates = {"hunger": 6.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
    for _ in range(60):
        apply_decay_tick(st, rates)
    # 6 points per sim hour
    assert st.needs["hunger"] == 94


def test_clamp_to_zero_discards_underflow():
    st = make_state(hunger=0)
    st.needsFraction["hunger"] = 0.0
    rates = {"hunger": 20.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
    apply_decay_tick(st, rates)
    assert st.needs["hunger"] == 0
    assert st.needsFraction["hunger"] == 0.0


def test_energy_recovery_while_sleeping():
    st = make_state(energy=50)
    # 12/hr recovery => 0.2 per tick; 60 ticks => +12
    for _ in range(60):
        apply_energy_recovery_tick(st, 12.0)
    assert st.needs["energy"] == 62


def test_recovery_clamped_at_100():
    st = make_state(energy=99)
    for _ in range(600):
        apply_energy_recovery_tick(st, 30.0)
    assert st.needs["energy"] == 100


def test_rate_bounds():
    assert clamp_rate(0.1, 0.5, 20.0) == 0.5
    assert clamp_rate(99, 0.5, 20.0) == 20.0
    assert clamp_rate(50, 1.0, 30.0) == 30.0


def test_critical_flag_set_and_cleared():
    st = make_state(hunger=15)
    flags = update_critical_flags(st)
    assert flags["hunger"] is True
    st.needs["hunger"] = 25
    flags = update_critical_flags(st)
    assert flags["hunger"] is False


def test_eat_recovery_requires_food_or_home_and_15min():
    st = make_state(hunger=30)
    assert on_eat_complete(st, 10, LocationCategory.FOOD, False) is False  # too short
    assert st.needs["hunger"] == 30
    assert on_eat_complete(st, 15, LocationCategory.RETAIL, False) is False  # wrong cat
    assert on_eat_complete(st, 20, LocationCategory.FOOD, False) is True
    assert st.needs["hunger"] == 70  # +40


def test_eat_at_home_counts():
    st = make_state(hunger=30)
    assert on_eat_complete(st, 20, LocationCategory.RESIDENCE, is_home=True) is True
    assert st.needs["hunger"] == 70


def test_social_recovery_once_per_conversation():
    st = make_state(social=40)
    assert on_conversation_complete(st, "c1", 6) is True
    assert st.needs["social"] == 55
    # same convo again => no further credit
    assert on_conversation_complete(st, "c1", 6) is False
    assert st.needs["social"] == 55
    # too short => no credit
    assert on_conversation_complete(st, "c2", 4) is False


def test_leisure_recovery_requires_30min():
    st = make_state(fun=40)
    assert on_leisure_complete(st, 20) is False
    assert on_leisure_complete(st, 30) is True
    assert st.needs["fun"] == 65


def test_critical_energy_travel_multiplier():
    st = make_state(energy=10)
    update_critical_flags(st)
    # 10 min travel * 1.5 = 15
    assert apply_critical_energy_travel(10, st) == 15
    # non-critical => unchanged
    st2 = make_state(energy=80)
    update_critical_flags(st2)
    assert apply_critical_energy_travel(10, st2) == 10
