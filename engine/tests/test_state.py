"""Tests for World_State_Serializer/Parser and DynamoStore (Requirement 13)."""
import pytest

from village.models import (Action, ActionType, Agent, AgentState, Config,
                            CrimeEvent, CrimeType, EmploymentStatus, Job,
                            LegalStatus, Location, LocationCategory,
                            OpeningHours, Outcome, Persona, Relationship,
                            TargetType)
from village.state import (DynamoStore, SchemaVersionError, World_State_Parser,
                           World_State_Serializer, from_decimal, to_decimal)


def sample_agent():
    state = AgentState(
        lat=-37.8, lon=144.9, presentLocationId="loc_home",
        needs={"hunger": 72, "energy": 68, "social": 70, "fun": 66},
        needsFraction={"hunger": 0.5, "energy": 0.0, "social": 0.0, "fun": 0.0},
        critical={"hunger": False, "energy": False, "social": False, "fun": False},
        cash=230.55, employmentStatus=EmploymentStatus.EMPLOYED,
        legalStatus=LegalStatus.CLEAR, jobId="job_1", dailyLivingCost=40.0,
        currentAction=Action(type=ActionType.IDLE, targetType=TargetType.LOCATION,
                             targetId="loc_home", expectedDurationMin=10))
    persona = Persona(name="Aroha Ngata", age=34, occupation="Barista",
                      traits=["warm", "impulsive", "curious"], background="bio",
                      homeLocationId="loc_home", wakeTime="07:00")
    return Agent(id="agent_01", persona=persona, state=state)


def sample_location():
    return Location(id="loc_fed", name="Federation Square",
                    category=LocationCategory.LEISURE, lat=-37.817979,
                    lon=144.968480, capacity=500,
                    hours=[OpeningHours("08:00", "23:00")] * 7,
                    isDetentionFacility=False)


def test_agent_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    a = sample_agent()
    item = ser.agent(a)
    assert item["PK"] == "SIM#melb"
    assert item["SK"] == "AGENT#agent_01"
    back = par.parse(item)
    assert back.id == a.id
    assert back.to_dict() == a.to_dict()


def test_location_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    loc = sample_location()
    item = ser.location(loc)
    assert item["SK"] == "LOC#loc_fed"
    back = par.parse(item)
    assert back.to_dict() == loc.to_dict()


def test_job_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    job = Job(id="job_1", locationId="loc_w", occupation="Barista",
              wagePerHour=32.50, shiftStart="07:00", shiftDurationHours=6,
              assignedAgentId="agent_01")
    back = par.parse(ser.job(job))
    assert back.to_dict() == job.to_dict()


def test_relationship_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    rel = Relationship(from_id="agent_01", to_id="agent_02", familiarity=12, sentiment=5)
    item = ser.relationship(rel)
    assert item["SK"] == "REL#agent_01#agent_02"
    back = par.parse(item)
    assert back.to_dict() == rel.to_dict()


def test_crime_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    c = CrimeEvent(id="uuid1", simTime="2026-03-02T14:00:00+11:00",
                   perpetrator="agent_01", crimeType=CrimeType.THEFT,
                   targetType=TargetType.AGENT, targetId="agent_02",
                   witnesses=["agent_03"], outcome=Outcome.SUCCEEDED,
                   stolenAmount=50)
    item = ser.crime(c)
    assert item["SK"].startswith("CRIME#")
    back = par.parse(item)
    assert back.to_dict() == c.to_dict()


def test_config_round_trip():
    ser = World_State_Serializer("melb")
    par = World_State_Parser()
    cfg = Config(simId="melb", detentionFacilityId="loc_remand")
    item = ser.config(cfg)
    assert item["SK"] == "CONFIG"
    back = par.parse(item)
    assert back.to_dict() == cfg.to_dict()


def test_version_mismatch_raises():
    par = World_State_Parser()
    item = {"PK": "SIM#melb", "SK": "AGENT#a1",
            "schemaVersion": 999, "id": "a1",
            "persona": sample_agent().persona.to_dict(),
            "state": sample_agent().state.to_dict(),
            "provenance": "generated"}
    with pytest.raises(SchemaVersionError) as e:
        par.parse(item)
    assert e.value.version == 999


def test_decimal_conversion_round_trip():
    obj = {"cash": 230.55, "n": 5, "b": True, "nested": {"x": 1.5}, "arr": [0.1, 2]}
    dec = to_decimal(obj)
    back = from_decimal(dec)
    assert back["cash"] == 230.55
    assert back["n"] == 5
    assert back["b"] is True
    assert back["nested"]["x"] == 1.5


# -- DynamoStore retry with a fake table -----------------------------------
class _TransientError(RuntimeError):
    """Mimics a botocore ClientError for a throttling/transient DynamoDB fault
    (carries the ``response.Error.Code`` structure the store inspects)."""
    def __init__(self, code="ThrottlingException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _PermanentError(RuntimeError):
    """Mimics a non-transient botocore ClientError (e.g. validation)."""
    def __init__(self, code="ValidationException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FlakyTable:
    def __init__(self, fail_times=0, error=None):
        self.fail_times = fail_times
        self.calls = 0
        self.items = []
        self._error = error or _TransientError()

    def put_item(self, Item=None, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._error
        self.items.append(Item)


def test_store_retries_then_succeeds():
    delays = []
    table = FlakyTable(fail_times=2)
    store = DynamoStore(table=table, sleep=lambda d: delays.append(d))
    store.put({"PK": "SIM#x", "SK": "AGENT#a"})
    assert table.calls == 3           # 2 failures + 1 success
    assert delays == [1.0, 2.0]       # backoff 1s, 2s


def test_store_raises_after_all_retries():
    table = FlakyTable(fail_times=99)
    store = DynamoStore(table=table, sleep=lambda d: None)
    with pytest.raises(RuntimeError):
        store.put({"PK": "SIM#x", "SK": "AGENT#a"})
    assert table.calls == 4           # 1 + 3 retries


def test_store_does_not_retry_non_transient_errors():
    """Validation/conditional-check style failures re-raise immediately with no
    blocking backoff (F7 remediation)."""
    delays = []
    table = FlakyTable(fail_times=99, error=_PermanentError())
    store = DynamoStore(table=table, sleep=lambda d: delays.append(d))
    with pytest.raises(RuntimeError):
        store.put({"PK": "SIM#x", "SK": "AGENT#a"})
    assert table.calls == 1           # no retries on a permanent error
    assert delays == []               # never slept on the tick thread
