"""World state persistence — serializer, parser, DynamoStore (Requirement 13).

Converts in-memory domain objects to/from DynamoDB items using the DESIGN.md §3
single-table key schema and provides an injectable DynamoStore wrapper with
retry/backoff. All floats are stored as Decimal for DynamoDB compatibility.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Protocol

from . import SUPPORTED_SCHEMA_VERSIONS
from .models import (Agent, Config, CrimeEvent, EventLogEntry, Job, Location,
                     Relationship)

WRITE_RETRIES = 3
RETRY_BACKOFF = [1.0, 2.0, 4.0]   # Req 13.8


class SchemaVersionError(Exception):
    def __init__(self, record_id: str, version: int):
        super().__init__(f"unsupported schemaVersion {version} for {record_id}")
        self.record_id = record_id
        self.version = version


# --------------------------------------------------------------------------
# Key helpers (DESIGN.md §3)
# --------------------------------------------------------------------------
def pk(sim_id: str) -> str:
    return f"SIM#{sim_id}"


def sk_config() -> str:
    return "CONFIG"


def sk_control() -> str:
    return "CONTROL"


def sk_status() -> str:
    return "STATUS"


def sk_clock() -> str:
    return "CLOCK"


def sk_location(loc_id: str) -> str:
    return f"LOC#{loc_id}"


def sk_agent(agent_id: str) -> str:
    return f"AGENT#{agent_id}"


def sk_job(job_id: str) -> str:
    return f"JOB#{job_id}"


def sk_relationship(from_id: str, to_id: str) -> str:
    return f"REL#{from_id}#{to_id}"


def sk_crime(ts: str, uid: str) -> str:
    return f"CRIME#{ts}#{uid}"


def sk_event(seq20: str) -> str:
    return f"EVENT#{seq20}"


def gsi1_event(sim_id: str, category: str, sim_time_iso: str, seq20: str):
    return (f"SIM#{sim_id}#CAT#{category}", f"{sim_time_iso}#{seq20}")


def gsi2_event(sim_id: str, agent_id: str, sim_time_iso: str, seq20: str):
    return (f"SIM#{sim_id}#AGENT#{agent_id}", f"{sim_time_iso}#{seq20}")


# --------------------------------------------------------------------------
# Float <-> Decimal conversion for DynamoDB
# --------------------------------------------------------------------------
def to_decimal(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_decimal(v) for v in obj]
    return obj


def from_decimal(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # preserve ints where exact
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_decimal(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# Serializer (Req 13.3)
# --------------------------------------------------------------------------
class World_State_Serializer:
    def __init__(self, sim_id: str):
        self.sim_id = sim_id

    def _wrap(self, sk: str, payload: Dict[str, Any],
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        item = {"PK": pk(self.sim_id), "SK": sk}
        item.update(payload)
        if extra:
            item.update(extra)
        return to_decimal(item)

    def location(self, loc: Location) -> Dict[str, Any]:
        return self._wrap(sk_location(loc.id), loc.to_dict())

    def agent(self, agent: Agent) -> Dict[str, Any]:
        return self._wrap(sk_agent(agent.id), agent.to_dict())

    def job(self, job: Job) -> Dict[str, Any]:
        return self._wrap(sk_job(job.id), job.to_dict())

    def relationship(self, rel: Relationship) -> Dict[str, Any]:
        return self._wrap(sk_relationship(rel.from_id, rel.to_id), rel.to_dict())

    def crime(self, crime: CrimeEvent) -> Dict[str, Any]:
        return self._wrap(sk_crime(crime.simTime, crime.id), crime.to_dict())

    def config(self, config: Config) -> Dict[str, Any]:
        return self._wrap(sk_config(), config.to_dict())

    def event(self, entry: EventLogEntry) -> Dict[str, Any]:
        from .eventlog import seq20 as _seq20
        s20 = _seq20(entry.seq)
        g1pk, g1sk = gsi1_event(self.sim_id, entry.category, entry.simTime, s20)
        extra: Dict[str, Any] = {"GSI1PK": g1pk, "GSI1SK": g1sk}
        # An event may involve multiple agents; GSI2 keyed on first agent.
        if entry.agents:
            g2pk, g2sk = gsi2_event(self.sim_id, entry.agents[0], entry.simTime, s20)
            extra["GSI2PK"] = g2pk
            extra["GSI2SK"] = g2sk
        return self._wrap(sk_event(s20), entry.to_dict(), extra)


# --------------------------------------------------------------------------
# Parser (Req 13.4 / 13.7)
# --------------------------------------------------------------------------
class World_State_Parser:
    def __init__(self, supported=None):
        self.supported = supported or SUPPORTED_SCHEMA_VERSIONS

    def _check_version(self, payload: Dict[str, Any], record_id: str) -> None:
        v = int(payload.get("schemaVersion", 1))
        if v not in self.supported:
            raise SchemaVersionError(record_id, v)

    def parse(self, item: Dict[str, Any]):
        """Dispatch on the SK prefix into the correct domain object."""
        item = from_decimal(item)
        sk = item.get("SK", "")
        payload = {k: v for k, v in item.items() if k not in ("PK", "SK",
                                                               "GSI1PK", "GSI1SK",
                                                               "GSI2PK", "GSI2SK")}
        if sk.startswith("LOC#"):
            self._check_version(payload, sk)
            return Location.from_dict(payload)
        if sk.startswith("AGENT#"):
            self._check_version(payload, sk)
            return Agent.from_dict(payload)
        if sk.startswith("JOB#"):
            self._check_version(payload, sk)
            return Job.from_dict(payload)
        if sk.startswith("REL#"):
            self._check_version(payload, sk)
            return Relationship.from_dict(payload)
        if sk.startswith("CRIME#"):
            self._check_version(payload, sk)
            return CrimeEvent.from_dict(payload)
        if sk.startswith("EVENT#"):
            self._check_version(payload, sk)
            return EventLogEntry.from_dict(payload)
        if sk == "CONFIG":
            self._check_version(payload, sk)
            return Config.from_dict(payload)
        raise ValueError(f"unknown SK prefix: {sk}")


# --------------------------------------------------------------------------
# DynamoStore (injectable / mockable)
# --------------------------------------------------------------------------
class TableLike(Protocol):
    def put_item(self, **kwargs) -> Any: ...
    def get_item(self, **kwargs) -> Any: ...
    def query(self, **kwargs) -> Any: ...


class DynamoStore:
    """Thin boto3 DynamoDB resource wrapper with retry/backoff.

    `table` may be injected (a fake) for tests; otherwise a boto3 Table is
    created from `table_name`/`region`.
    """

    def __init__(self, table_name: str = "village", region: str = "ap-southeast-2",
                 table: Optional[TableLike] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.table_name = table_name
        self._sleep = sleep
        if table is not None:
            self.table = table
        else:
            import boto3  # imported lazily so tests need no AWS
            self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- write with retry (Req 13.8 / 13.11) -------------------------------
    def put(self, item: Dict[str, Any]) -> None:
        attempts = 0
        while True:
            try:
                self.table.put_item(Item=item)
                return
            except Exception:
                if attempts >= WRITE_RETRIES:
                    raise
                self._sleep(RETRY_BACKOFF[attempts])
                attempts += 1

    def get(self, sim_id: str, sk: str) -> Optional[Dict[str, Any]]:
        resp = self.table.get_item(Key={"PK": pk(sim_id), "SK": sk})
        return resp.get("Item")

    def query_pk(self, sim_id: str, sk_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(pk(sim_id))}
        if sk_prefix:
            kwargs["KeyConditionExpression"] = Key("PK").eq(pk(sim_id)) & Key("SK").begins_with(sk_prefix)
        return self._paginated(kwargs)

    def query_events_by_category(self, sim_id: str, category: str,
                                 from_iso: Optional[str] = None,
                                 to_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key
        cond = Key("GSI1PK").eq(f"SIM#{sim_id}#CAT#{category}")
        if from_iso and to_iso:
            cond = cond & Key("GSI1SK").between(from_iso, to_iso + "#\uffff")
        return self._paginated({"IndexName": "GSI1", "KeyConditionExpression": cond})

    def query_events_by_agent(self, sim_id: str, agent_id: str,
                              from_iso: Optional[str] = None,
                              to_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key
        cond = Key("GSI2PK").eq(f"SIM#{sim_id}#AGENT#{agent_id}")
        if from_iso and to_iso:
            cond = cond & Key("GSI2SK").between(from_iso, to_iso + "#\uffff")
        return self._paginated({"IndexName": "GSI2", "KeyConditionExpression": cond})

    def _paginated(self, kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        resp = self.table.query(**kwargs)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items", []))
        return items


__all__ = [
    "World_State_Serializer", "World_State_Parser", "DynamoStore",
    "SchemaVersionError", "to_decimal", "from_decimal",
    "pk", "sk_config", "sk_control", "sk_status", "sk_clock",
    "sk_location", "sk_agent", "sk_job", "sk_relationship", "sk_crime", "sk_event",
    "gsi1_event", "gsi2_event", "WRITE_RETRIES", "RETRY_BACKOFF",
]
