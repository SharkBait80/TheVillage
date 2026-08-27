"""Tests for the Simulation_Controller (Requirement 2 / 4 / 18)."""
import pytest

from village.controller import (ConfigValidationError, POPULATION_DEFAULT,
                                Simulation_Controller, validate_config,
                                validate_population)
from village.models import Budget, Config, ModelPrice, SimStatus

OPUS = "au.anthropic.claude-opus-5"
HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def make_config():
    return Config(simId="melb", detentionFacilityId="loc_remand",
                  budget=Budget(prices={OPUS: ModelPrice(0.015, 0.075),
                                        HAIKU: ModelPrice(0.0008, 0.004)}))


def test_population_default():
    assert validate_population(None) == POPULATION_DEFAULT


def test_population_out_of_range_rejected():
    with pytest.raises(ConfigValidationError):
        validate_population(4)
    with pytest.raises(ConfigValidationError):
        validate_population(2001)
    with pytest.raises(ConfigValidationError):
        validate_population(True)


def test_population_large_scale_accepted():
    # The world scales to hundreds of agents (500+); the controller must
    # accept large populations so the engine can start.
    assert validate_population(500) == 500
    assert validate_population(2000) == 2000


def test_validate_config_ok():
    validate_config(make_config())


def test_validate_config_requires_detention():
    cfg = make_config()
    cfg.detentionFacilityId = None
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_start_from_stopped():
    ctrl = Simulation_Controller(make_config())
    assert ctrl.status == SimStatus.STOPPED
    res = ctrl.start()
    assert res.accepted
    assert ctrl.status == SimStatus.RUNNING


def test_start_forbidden_when_running():
    ctrl = Simulation_Controller(make_config())
    ctrl.start()
    res = ctrl.start()
    assert not res.accepted
    assert "forbidden" in res.message
    assert ctrl.status == SimStatus.RUNNING


def test_pause_resume_cycle():
    ctrl = Simulation_Controller(make_config())
    ctrl.start()
    assert ctrl.pause().accepted
    assert ctrl.status == SimStatus.PAUSED
    # pause again forbidden
    assert not ctrl.pause().accepted
    assert ctrl.resume().accepted
    assert ctrl.status == SimStatus.RUNNING


def test_resume_forbidden_when_running():
    ctrl = Simulation_Controller(make_config())
    ctrl.start()
    res = ctrl.resume()
    assert not res.accepted


def test_stop_from_running_and_paused():
    ctrl = Simulation_Controller(make_config())
    ctrl.start()
    assert ctrl.stop().accepted
    assert ctrl.status == SimStatus.STOPPED
    # stop again forbidden
    assert not ctrl.stop().accepted


def test_apply_dispatch():
    ctrl = Simulation_Controller(make_config())
    assert ctrl.apply("start").accepted
    assert ctrl.apply("pause").accepted
    assert ctrl.apply("resume").accepted
    assert ctrl.apply("stop").accepted
    assert not ctrl.apply("bogus").accepted


def test_spend_cap_pause_and_reject_resume():
    ctrl = Simulation_Controller(make_config())
    ctrl.start()
    res = ctrl.pause_for_spend()
    assert res.accepted
    assert ctrl.status == SimStatus.PAUSED
    rej = ctrl.reject_resume_spend_capped()
    assert not rej.accepted
    assert "maxSpendUSD" in rej.message
