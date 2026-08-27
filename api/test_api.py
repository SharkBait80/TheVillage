"""Unit tests for the Simulation_API Lambda handler.

Verifies (without deploying) against DESIGN §5 + requirements.md:
  - method+path routing for every endpoint,
  - auth rejection: missing JWT claims -> 401 and NO DynamoDB access (Req 17.5),
  - response envelope shape {ok, data|error},
  - control writes a CONTROL item + rejects invalid commands (Req 2),
  - cost-report aggregation math by modelId/purpose, USD 2dp, zeros-not-error (Req 18.9),
  - summary aggregation window (Req 14.9),
  - event query <=500 + more flag + empty result (Req 14.2/14.3),
  - decision-trail present + absent (Req 14.4/14.5),
  - agent/location detail (Req 15.6/15.7),
  - asset serving 302 + 404 when absent (Req 16.6/16.10),
  - config validation ranges (Req 4 / 18.1/18.2).

boto3 is monkeypatched with an in-memory fake table/S3/Lambda: the handler's
lazy client accessors are replaced so no AWS calls occur.
"""

import importlib
import json
import os
import sys
from decimal import Decimal

import pytest

# Import the handler module (index.py in this dir).
sys.path.insert(0, os.path.dirname(__file__))
import index  # noqa: E402


# --------------------------------------------------------------------------- #
# In-memory fakes
# --------------------------------------------------------------------------- #

class FakeTable:
    """Minimal DynamoDB Table supporting get_item, put_item, query (base+GSI)."""

    def __init__(self):
        self.items = {}  # (PK, SK) -> item
        self.calls = []  # record access for the auth "no read/write" assertion

    # -- key helpers --------------------------------------------------------
    def put_item(self, Item=None, **_):
        self.calls.append("put_item")
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key=None, **_):
        self.calls.append("get_item")
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs):
        self.calls.append("query")
        index_name = kwargs.get("IndexName")
        cond = kwargs["KeyConditionExpression"]
        # We introspect the boto3 condition object to extract the target values.
        results = []
        if index_name == "GSI1":
            want = _eq_value(cond)
            results = [i for i in self.items.values() if i.get("GSI1PK") == want]
        elif index_name == "GSI2":
            want = _eq_value(cond)
            results = [i for i in self.items.values() if i.get("GSI2PK") == want]
        else:
            pk, prefix = _pk_prefix(cond)
            results = [
                i for i in self.items.values()
                if i.get("PK") == pk and str(i.get("SK", "")).startswith(prefix)
            ]
        return {"Items": [dict(i) for i in results]}


def _eq_value(cond):
    """Extract the RHS value from a boto3 Key('x').eq(value) condition."""
    # boto3 conditions expose get_expression()
    expr = cond.get_expression()
    return expr["values"][1]


def _pk_prefix(cond):
    """Extract (pk_value, sk_prefix) from Key('PK').eq(pk) & Key('SK').begins_with(pfx)."""
    expr = cond.get_expression()
    # 'AND' of two conditions
    left, right = expr["values"]
    pk = _eq_value(left)
    right_expr = right.get_expression()
    prefix = right_expr["values"][1]
    return pk, prefix


class FakeS3:
    def __init__(self, existing_keys=None, fail_head=False):
        self.existing = set(existing_keys or [])
        self.fail_head = fail_head

    def head_object(self, Bucket=None, Key=None, **_):
        if self.fail_head or Key not in self.existing:
            raise RuntimeError("NoSuchKey")
        return {"ContentLength": 123}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None, **_):
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=abc"


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, FunctionName=None, InvocationType=None, Payload=None, **_):
        self.invocations.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload.decode("utf-8")),
        })
        return {"StatusCode": 202}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(index, "_table", lambda: t)
    return t


@pytest.fixture
def fake_s3(monkeypatch):
    s = FakeS3()
    monkeypatch.setattr(index, "_s3", lambda: s)
    return s


@pytest.fixture
def fake_lambda(monkeypatch):
    l = FakeLambda()
    monkeypatch.setattr(index, "_lambda", lambda: l)
    return l


@pytest.fixture(autouse=True)
def _bucket_env(monkeypatch):
    monkeypatch.setattr(index, "ASSETS_BUCKET", "village-assets")
    monkeypatch.setattr(index, "ASSET_FN_NAME", "AssetGenFn")
    monkeypatch.setattr(index, "ALLOW_ANON", False)


# --------------------------------------------------------------------------- #
# Event builders (API Gateway HTTP API payload format 2.0)
# --------------------------------------------------------------------------- #

def make_event(method, path, body=None, query=None, authed=True):
    authorizer = {}
    if authed:
        authorizer = {"jwt": {"claims": {"sub": "user-123", "email": "op@example.com"}}}
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": authorizer,
        },
    }


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"]) if resp.get("body") else {}


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #

def seed_status(t, sim="melb", sim_time="2026-03-02T14:30:00+11:00", status="running", accel=4):
    t.put_item(Item={"PK": f"SIM#{sim}", "SK": "STATUS", "status": status,
                     "simTime": sim_time, "accel": accel, "updatedAt": sim_time})


def seed_agent(t, sim="melb", agent_id="agent_01", name="Aroha Ngata",
               legal="clear", employment="employed", lat=-37.81, lon=144.96,
               present=None, action_type="idle"):
    t.put_item(Item={
        "PK": f"SIM#{sim}", "SK": f"AGENT#{agent_id}", "id": agent_id,
        "provenance": "generated",
        "persona": {"name": name, "age": 34, "occupation": "Barista"},
        "state": {
            "lat": Decimal(str(lat)), "lon": Decimal(str(lon)),
            "presentLocationId": present,
            "legalStatus": legal, "employmentStatus": employment,
            "cash": Decimal("230.00"),
            "currentAction": {"type": action_type, "route": None},
        },
    })


def seed_location(t, sim="melb", loc_id="loc_fed", name="Fed Square", category="leisure",
                  lat=-37.817979, lon=144.96848, capacity=500, hours=None):
    if hours is None:
        hours = [{"open": "08:00", "close": "23:00"} for _ in range(7)]
    t.put_item(Item={
        "PK": f"SIM#{sim}", "SK": f"LOC#{loc_id}", "id": loc_id, "name": name,
        "category": category, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)),
        "capacity": capacity, "hours": hours, "isDetentionFacility": False,
    })


def seed_event(t, sim="melb", seq=1, category="action", sim_time="2026-03-02T09:00:00+11:00",
               agents=None, detail=None, description="did a thing", location_id="loc_fed"):
    agents = agents or []
    item = {
        "PK": f"SIM#{sim}", "SK": f"EVENT#{str(seq).zfill(20)}",
        "seq": seq, "simTime": sim_time, "realTime": sim_time,
        "category": category, "agents": agents, "locationId": location_id,
        "description": description, "detail": detail,
        "GSI1PK": f"SIM#{sim}#CAT#{category}", "GSI1SK": f"{sim_time}#{seq}",
    }
    if agents:
        item["GSI2PK"] = f"SIM#{sim}#AGENT#{agents[0]}"
        item["GSI2SK"] = f"{sim_time}#{seq}"
    t.put_item(Item=item)


# --------------------------------------------------------------------------- #
# AUTH (Req 17.4 / 17.5)
# --------------------------------------------------------------------------- #

def test_missing_claims_rejected_401_and_no_db_access(fake_table):
    ev = make_event("GET", "/v1/sim/melb/state", authed=False)
    status, body = parse(index.handler(ev))
    assert status == 401
    assert body["ok"] is False and "error" in body
    # Req 17.5: neither read nor modify any World_State_Record.
    assert fake_table.calls == []


def test_empty_claims_object_rejected(fake_table):
    ev = make_event("GET", "/v1/sim/melb/state", authed=True)
    ev["requestContext"]["authorizer"] = {"jwt": {"claims": {}}}
    status, body = parse(index.handler(ev))
    assert status == 401
    assert fake_table.calls == []


def test_allow_anon_escape_hatch(monkeypatch, fake_table):
    monkeypatch.setattr(index, "ALLOW_ANON", True)
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/state", authed=False)
    status, body = parse(index.handler(ev))
    assert status == 200 and body["ok"] is True


def test_authed_request_allows_access(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/state")
    status, body = parse(index.handler(ev))
    assert status == 200 and body["ok"] is True


# --------------------------------------------------------------------------- #
# ROUTING & envelope
# --------------------------------------------------------------------------- #

def test_options_preflight_no_auth():
    ev = make_event("OPTIONS", "/v1/sim/melb/state", authed=False)
    status, _ = parse(index.handler(ev))
    assert status == 204


def test_unknown_route_404(fake_table):
    ev = make_event("GET", "/v1/sim/melb/nope")
    status, body = parse(index.handler(ev))
    assert status == 404 and body["ok"] is False


def test_cors_headers_present(fake_table):
    seed_status(fake_table)
    resp = index.handler(make_event("GET", "/v1/sim/melb/state"))
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


# --------------------------------------------------------------------------- #
# CONTROL (Req 2)
# --------------------------------------------------------------------------- #

def test_control_writes_control_item(fake_table):
    seed_status(fake_table, status="stopped")
    ev = make_event("POST", "/v1/sim/melb/control", body={"command": "start"})
    status, body = parse(index.handler(ev))
    assert status == 200 and body["ok"] is True
    assert body["data"]["accepted"] is True
    ctrl = fake_table.items[("SIM#melb", "CONTROL")]
    assert ctrl["command"] == "start"
    assert ctrl["nonce"] and ctrl["requestedAt"]
    # returns current status (Req 2 / DESIGN §5)
    assert body["data"]["status"]["status"] == "stopped"


def test_control_rejects_invalid_command(fake_table):
    seed_status(fake_table)
    ev = make_event("POST", "/v1/sim/melb/control", body={"command": "explode"})
    status, body = parse(index.handler(ev))
    assert status == 400 and body["ok"] is False
    # No CONTROL item written on rejection.
    assert ("SIM#melb", "CONTROL") not in fake_table.items


# --------------------------------------------------------------------------- #
# STATE
# --------------------------------------------------------------------------- #

def test_state_shape(fake_table):
    seed_status(fake_table)
    seed_agent(fake_table, agent_id="agent_01", name="Aroha")
    seed_agent(fake_table, agent_id="agent_02", name="Ben", legal="suspected")
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/state")))
    assert status == 200
    d = body["data"]
    assert d["status"] == "running" and d["accel"] == 4
    assert len(d["agents"]) == 2
    a = {x["id"]: x for x in d["agents"]}
    assert a["agent_01"]["name"] == "Aroha"
    assert set(a["agent_01"].keys()) == {"id", "name", "lat", "lon", "action", "route", "legal", "employment"}


# --------------------------------------------------------------------------- #
# AGENT DETAIL (Req 15.6)
# --------------------------------------------------------------------------- #

def test_agent_detail_with_recent_events(fake_table):
    seed_status(fake_table)
    seed_agent(fake_table, agent_id="agent_01")
    for i in range(1, 13):
        seed_event(fake_table, seq=i, agents=["agent_01"],
                   sim_time=f"2026-03-02T09:{i:02d}:00+11:00")
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/agents/agent_01")))
    assert status == 200
    d = body["data"]
    assert d["persona"]["name"] == "Aroha Ngata"
    assert d["state"]["legalStatus"] == "clear"
    # last 10 only, ascending
    assert len(d["recentEvents"]) == 10
    seqs = [e["seq"] for e in d["recentEvents"]]
    assert seqs == sorted(seqs) and seqs[-1] == 12


def test_agent_detail_unknown_404(fake_table):
    seed_status(fake_table)
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/agents/ghost")))
    assert status == 404 and body["ok"] is False


# --------------------------------------------------------------------------- #
# LOCATIONS (Req 15.7 / Req 3 status)
# --------------------------------------------------------------------------- #

def test_locations_status_open_and_present_agents(fake_table):
    # Monday 2026-03-02 14:30 Melbourne -> within 08:00-23:00 => open.
    seed_status(fake_table, sim_time="2026-03-02T14:30:00+11:00")
    seed_location(fake_table, loc_id="loc_fed", capacity=500)
    seed_agent(fake_table, agent_id="agent_01", present="loc_fed")
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/locations")))
    assert status == 200
    locs = {l["id"]: l for l in body["data"]["locations"]}
    assert locs["loc_fed"]["status"] == "open"
    assert locs["loc_fed"]["occupancy"] == 1
    assert locs["loc_fed"]["presentAgents"][0]["id"] == "agent_01"


def test_location_closed_outside_hours(fake_table):
    # 03:00 is outside 08:00-23:00 => closed.
    seed_status(fake_table, sim_time="2026-03-02T03:00:00+11:00")
    seed_location(fake_table, loc_id="loc_fed")
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/locations/loc_fed")))
    assert status == 200
    assert body["data"]["status"] == "closed"


def test_location_at_capacity(fake_table):
    seed_status(fake_table, sim_time="2026-03-02T14:30:00+11:00")
    seed_location(fake_table, loc_id="loc_small", capacity=1)
    seed_agent(fake_table, agent_id="a1", present="loc_small")
    seed_agent(fake_table, agent_id="a2", present="loc_small")
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/locations/loc_small")))
    assert body["data"]["status"] == "at_capacity"


# --------------------------------------------------------------------------- #
# EVENTS (Req 14.2 / 14.3)
# --------------------------------------------------------------------------- #

def test_events_filter_by_category_ascending(fake_table):
    seed_status(fake_table)
    seed_event(fake_table, seq=3, category="crime", sim_time="2026-03-02T10:00:00+11:00")
    seed_event(fake_table, seq=1, category="crime", sim_time="2026-03-02T08:00:00+11:00")
    seed_event(fake_table, seq=2, category="action", sim_time="2026-03-02T09:00:00+11:00")
    ev = make_event("GET", "/v1/sim/melb/events", query={"category": "crime"})
    status, body = parse(index.handler(ev))
    assert status == 200
    evs = body["data"]["events"]
    assert [e["seq"] for e in evs] == [1, 3]  # ascending, only crimes
    assert body["data"]["more"] is False


def test_events_empty_result_not_error(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/events", query={"category": "crime"})
    status, body = parse(index.handler(ev))
    assert status == 200 and body["ok"] is True
    assert body["data"]["events"] == [] and body["data"]["more"] is False


def test_events_more_flag_and_cap(fake_table, monkeypatch):
    monkeypatch.setattr(index, "MAX_EVENTS", 2)
    seed_status(fake_table)
    for i in range(1, 6):
        seed_event(fake_table, seq=i, category="action",
                   sim_time=f"2026-03-02T09:{i:02d}:00+11:00")
    ev = make_event("GET", "/v1/sim/melb/events", query={"category": "action"})
    status, body = parse(index.handler(ev))
    assert len(body["data"]["events"]) == 2
    assert body["data"]["more"] is True
    assert body["data"]["nextCursor"] == 2


def test_events_filter_by_agent_and_time(fake_table):
    seed_status(fake_table)
    seed_event(fake_table, seq=1, agents=["agent_01"], sim_time="2026-03-02T08:00:00+11:00")
    seed_event(fake_table, seq=2, agents=["agent_01"], sim_time="2026-03-02T12:00:00+11:00")
    seed_event(fake_table, seq=3, agents=["agent_02"], sim_time="2026-03-02T09:00:00+11:00")
    ev = make_event("GET", "/v1/sim/melb/events",
                    query={"agentId": "agent_01", "fromSimTime": "2026-03-02T10:00:00+11:00"})
    status, body = parse(index.handler(ev))
    seqs = [e["seq"] for e in body["data"]["events"]]
    assert seqs == [2]  # agent_01 AND after 10:00


# --------------------------------------------------------------------------- #
# DECISION TRAIL (Req 14.4 / 14.5)
# --------------------------------------------------------------------------- #

def test_decision_trail_present(fake_table):
    seed_status(fake_table)
    seed_event(fake_table, seq=7, category="action",
               detail={"perceptionInput": {"needs": {"hunger": 30}},
                       "retrievedMemoryIds": ["m1", "m2"],
                       "action": {"type": "eat"}})
    ev = make_event("GET", "/v1/sim/melb/events/decision-trail",
                    query={"actionEventSeq": "7"})
    status, body = parse(index.handler(ev))
    assert status == 200
    assert body["data"]["retrievedMemoryIds"] == ["m1", "m2"]
    assert body["data"]["action"]["type"] == "eat"


def test_decision_trail_absent(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/events/decision-trail",
                    query={"actionEventSeq": "999"})
    status, body = parse(index.handler(ev))
    assert status == 404 and body["ok"] is False
    assert body["data"] is None


def test_decision_trail_bad_seq(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/events/decision-trail",
                    query={"actionEventSeq": "abc"})
    status, body = parse(index.handler(ev))
    assert status == 400 and body["ok"] is False


def test_decision_trail_includes_reasoning(fake_table):
    seed_status(fake_table)
    seed_event(fake_table, seq=8, category="action",
               detail={"kind": "accepted",
                       "reasoning": "I was hungry so I chose to eat",
                       "perceptionInput": {"needs": {"hunger": 20}, "locationId": "loc_fed"},
                       "action": {"type": "eat"}})
    ev = make_event("GET", "/v1/sim/melb/events/decision-trail",
                    query={"actionEventSeq": "8"})
    status, body = parse(index.handler(ev))
    assert status == 200
    assert body["data"]["reasoning"] == "I was hungry so I chose to eat"
    assert body["data"]["perceptionInput"]["locationId"] == "loc_fed"


# --------------------------------------------------------------------------- #
# CONVERSATIONS (agent-to-agent transcripts)
# --------------------------------------------------------------------------- #

def _seed_conversation(t, seq, sim_time, conv_id, participants, utterances,
                       location_id="loc_fed"):
    seed_event(
        t, seq=seq, category="conversation", sim_time=sim_time,
        agents=participants, location_id=location_id,
        description=f"conversation-ended at {location_id}",
        detail={
            "kind": "conversation-ended",
            "conversationId": conv_id,
            "participants": participants,
            "locationId": location_id,
            "utterances": utterances,
            "truncated": False,
            "utteranceCount": len(utterances),
        })


def test_conversations_list_returns_transcripts(fake_table):
    seed_status(fake_table)
    _seed_conversation(
        fake_table, seq=10, sim_time="2026-03-02T09:05:00+11:00",
        conv_id="c1", participants=["agent_01", "agent_02"],
        utterances=[{"speaker": "agent_01", "text": "Morning!"},
                    {"speaker": "agent_02", "text": "Hey there!"}])
    ev = make_event("GET", "/v1/sim/melb/conversations")
    status, body = parse(index.handler(ev))
    assert status == 200
    convos = body["data"]["conversations"]
    assert len(convos) == 1
    c = convos[0]
    assert c["id"] == "c1"
    assert set(c["participants"]) == {"agent_01", "agent_02"}
    assert c["utterances"][0] == {"speaker": "agent_01", "text": "Morning!"}
    assert c["utteranceCount"] == 2


def test_conversations_list_empty_is_ok(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/conversations")
    status, body = parse(index.handler(ev))
    assert status == 200
    assert body["data"]["conversations"] == []


def test_conversations_filter_by_agent(fake_table):
    seed_status(fake_table)
    _seed_conversation(fake_table, seq=11, sim_time="2026-03-02T09:05:00+11:00",
                       conv_id="c1", participants=["agent_01", "agent_02"],
                       utterances=[{"speaker": "agent_01", "text": "hi"},
                                   {"speaker": "agent_02", "text": "yo"}])
    ev = make_event("GET", "/v1/sim/melb/conversations",
                    query={"agentId": "agent_01"})
    status, body = parse(index.handler(ev))
    assert status == 200
    assert len(body["data"]["conversations"]) == 1


def test_conversation_detail_by_id(fake_table):
    seed_status(fake_table)
    _seed_conversation(fake_table, seq=12, sim_time="2026-03-02T09:05:00+11:00",
                       conv_id="c-detail", participants=["agent_03", "agent_04"],
                       utterances=[{"speaker": "agent_03", "text": "one"},
                                   {"speaker": "agent_04", "text": "two"}])
    ev = make_event("GET", "/v1/sim/melb/conversations/c-detail")
    status, body = parse(index.handler(ev))
    assert status == 200
    assert body["data"]["id"] == "c-detail"
    assert len(body["data"]["utterances"]) == 2


def test_conversation_detail_unknown_404(fake_table):
    seed_status(fake_table)
    ev = make_event("GET", "/v1/sim/melb/conversations/nope")
    status, body = parse(index.handler(ev))
    assert status == 404 and body["ok"] is False



# --------------------------------------------------------------------------- #
# SUMMARY (Req 14.9)
# --------------------------------------------------------------------------- #

def test_summary_aggregation(fake_table):
    seed_status(fake_table, sim_time="2026-03-02T14:30:00+11:00")
    seed_agent(fake_table, agent_id="a1", legal="clear", employment="employed")
    seed_agent(fake_table, agent_id="a2", legal="suspected", employment="unemployed")
    seed_agent(fake_table, agent_id="a3", legal="detained", employment="suspended")
    # crimes today + one yesterday (excluded)
    seed_event(fake_table, seq=1, category="crime", sim_time="2026-03-02T09:00:00+11:00")
    seed_event(fake_table, seq=2, category="crime", sim_time="2026-03-02T10:00:00+11:00")
    seed_event(fake_table, seq=3, category="crime", sim_time="2026-03-01T10:00:00+11:00")
    # conversations
    seed_event(fake_table, seq=4, category="conversation",
               sim_time="2026-03-02T11:00:00+11:00",
               detail={"kind": "conversation-ended"})
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/summary")))
    assert status == 200
    d = body["data"]
    assert d["legalStatusCounts"] == {"clear": 1, "suspected": 1, "charged": 0, "detained": 1}
    assert d["employmentStatusCounts"] == {"employed": 1, "unemployed": 1, "suspended": 1}
    assert d["crimeCount"] == 2  # only today's crimes
    assert d["conversationCount"] == 1


# --------------------------------------------------------------------------- #
# COST REPORT (Req 18.9)
# --------------------------------------------------------------------------- #

def test_cost_report_aggregation_math(fake_table):
    seed_status(fake_table)
    # config prices: opus per1kInput 0.015, per1kOutput 0.075
    fake_table.put_item(Item={
        "PK": "SIM#melb", "SK": "CONFIG",
        "budget": {"prices": {
            "au.anthropic.claude-opus-5": {"per1kInput": Decimal("0.015"),
                                           "per1kOutput": Decimal("0.075")},
            "au.anthropic.claude-haiku-4-5-20251001-v1:0": {"per1kInput": Decimal("0.001"),
                                                            "per1kOutput": Decimal("0.005")},
        }},
    })
    # two decision cycles on opus + one conversation on haiku
    seed_event(fake_table, seq=1, category="model", sim_time="2026-03-02T09:00:00+11:00",
               detail={"modelId": "au.anthropic.claude-opus-5", "purpose": "decision_cycle",
                       "inputTokens": 1000, "outputTokens": 500})
    seed_event(fake_table, seq=2, category="model", sim_time="2026-03-02T09:05:00+11:00",
               detail={"modelId": "au.anthropic.claude-opus-5", "purpose": "decision_cycle",
                       "inputTokens": 2000, "outputTokens": 1000})
    seed_event(fake_table, seq=3, category="model", sim_time="2026-03-02T09:10:00+11:00",
               detail={"modelId": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
                       "purpose": "conversation",
                       "inputTokens": 4000, "outputTokens": 2000})
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/cost")))
    assert status == 200
    d = body["data"]
    assert d["invocationCount"] == 3
    assert d["inputTokens"] == 7000
    assert d["outputTokens"] == 3500
    # opus: (1+2)*0.015 + (0.5+1.0)*0.075 = 0.045 + 0.1125 = 0.1575
    # haiku: 4*0.001 + 2*0.005 = 0.004 + 0.010 = 0.014
    # total = 0.1715 -> 0.17 (2dp)
    assert d["byModel"]["au.anthropic.claude-opus-5"]["estimatedSpendUSD"] == 0.16  # 0.1575->0.16
    assert d["byModel"]["au.anthropic.claude-haiku-4-5-20251001-v1:0"]["estimatedSpendUSD"] == 0.01
    assert d["byPurpose"]["decision_cycle"]["invocationCount"] == 2
    assert d["byPurpose"]["conversation"]["inputTokens"] == 4000
    # total spend rounded to 2dp
    assert d["estimatedSpendUSD"] == 0.17


def test_cost_report_zeros_not_error(fake_table):
    seed_status(fake_table)
    status, body = parse(index.handler(make_event("GET", "/v1/sim/melb/cost")))
    assert status == 200 and body["ok"] is True
    d = body["data"]
    assert d["invocationCount"] == 0
    assert d["estimatedSpendUSD"] == 0.0
    assert d["byModel"] == {} and d["byPurpose"] == {}


# --------------------------------------------------------------------------- #
# ASSETS (Req 16.6 / 16.10)
# --------------------------------------------------------------------------- #

def test_asset_get_302_redirect(fake_table, fake_s3):
    seed_status(fake_table)
    key = "sim/melb/agent/agent_01.png"
    fake_table.put_item(Item={"PK": "SIM#melb", "SK": "ASSET#agent_01",
                              "subjectId": "agent_01", "imageKey": key})
    fake_s3.existing.add(key)
    resp = index.handler(make_event("GET", "/v1/sim/melb/assets/agent_01"))
    assert resp["statusCode"] == 302
    assert "Location" in resp["headers"]
    assert key in resp["headers"]["Location"]


def test_asset_get_no_manifest_404(fake_table, fake_s3):
    seed_status(fake_table)
    resp = index.handler(make_event("GET", "/v1/sim/melb/assets/ghost"))
    status, body = parse(resp)
    assert status == 404 and body["ok"] is False


def test_asset_get_object_missing_404(fake_table, fake_s3):
    seed_status(fake_table)
    fake_table.put_item(Item={"PK": "SIM#melb", "SK": "ASSET#agent_01",
                              "subjectId": "agent_01",
                              "imageKey": "sim/melb/agent/agent_01.png"})
    # object not registered in fake_s3.existing -> head_object raises -> 404
    resp = index.handler(make_event("GET", "/v1/sim/melb/assets/agent_01"))
    status, body = parse(resp)
    assert status == 404 and body["ok"] is False


# --------------------------------------------------------------------------- #
# ASSET GENERATE
# --------------------------------------------------------------------------- #

def test_asset_generate_async_invoke_all(fake_table, fake_lambda):
    seed_status(fake_table)
    resp = index.handler(make_event("POST", "/v1/sim/melb/assets/generate", body={}))
    status, body = parse(resp)
    assert status == 202 and body["ok"] is True
    assert len(fake_lambda.invocations) == 1
    inv = fake_lambda.invocations[0]
    assert inv["InvocationType"] == "Event"
    assert inv["Payload"]["action"] == "generate_all"


def test_asset_generate_regenerate_subject(fake_table, fake_lambda):
    seed_status(fake_table)
    resp = index.handler(make_event("POST", "/v1/sim/melb/assets/generate",
                                    body={"subjectId": "agent_01"}))
    status, body = parse(resp)
    assert status == 202
    inv = fake_lambda.invocations[0]
    assert inv["Payload"]["action"] == "regenerate"
    assert inv["Payload"]["subjectId"] == "agent_01"


# --------------------------------------------------------------------------- #
# CONFIG (Req 4 / 18.1 / 18.2)
# --------------------------------------------------------------------------- #

def _valid_config():
    return {
        "population": 25,
        "accelerationFactor": 4,
        "startSimTime": "2026-03-02T06:00:00+11:00",
        "detentionFacilityId": "loc_remand",
        "budget": {
            "maxInvocationsPerSimHour": 5000,
            "maxSpendUSD": 25.0,
            "prices": {
                "au.anthropic.claude-opus-5": {"per1kInput": 0.015, "per1kOutput": 0.075},
            },
        },
    }


def test_config_valid_stores_and_flags_seed(fake_table):
    seed_status(fake_table)
    resp = index.handler(make_event("POST", "/v1/sim/melb/config", body=_valid_config()))
    status, body = parse(resp)
    assert status == 200 and body["ok"] is True
    assert body["data"]["seedPending"] is True
    stored = fake_table.items[("SIM#melb", "CONFIG")]
    assert stored["population"] == 25
    assert stored["seedPending"] is True
    # numbers persisted as Decimal for DynamoDB
    assert isinstance(stored["budget"]["maxSpendUSD"], Decimal)


def test_config_rejects_population_out_of_range(fake_table):
    cfg = _valid_config()
    cfg["population"] = 3
    resp = index.handler(make_event("POST", "/v1/sim/melb/config", body=cfg))
    status, body = parse(resp)
    assert status == 400 and body["ok"] is False
    fields = [e["field"] for e in body["errors"]]
    assert "population" in fields
    # nothing stored on rejection
    assert ("SIM#melb", "CONFIG") not in fake_table.items


def test_config_rejects_bad_budget(fake_table):
    cfg = _valid_config()
    cfg["budget"]["maxSpendUSD"] = 999999  # > 10000
    cfg["budget"]["maxInvocationsPerSimHour"] = 0  # < 1
    resp = index.handler(make_event("POST", "/v1/sim/melb/config", body=cfg))
    status, body = parse(resp)
    assert status == 400
    fields = [e["field"] for e in body["errors"]]
    assert "budget.maxSpendUSD" in fields
    assert "budget.maxInvocationsPerSimHour" in fields


def test_config_rejects_missing_price_for_model(fake_table):
    cfg = _valid_config()
    cfg["modelsUsed"] = ["au.anthropic.claude-opus-5", "au.anthropic.claude-haiku-4-5-20251001-v1:0"]
    # haiku price missing
    resp = index.handler(make_event("POST", "/v1/sim/melb/config", body=cfg))
    status, body = parse(resp)
    assert status == 400
    fields = [e["field"] for e in body["errors"]]
    assert any("haiku" in f for f in fields)


def test_config_rejects_price_out_of_range(fake_table):
    cfg = _valid_config()
    cfg["budget"]["prices"]["au.anthropic.claude-opus-5"]["per1kInput"] = 5000  # > 1000
    resp = index.handler(make_event("POST", "/v1/sim/melb/config", body=cfg))
    status, body = parse(resp)
    assert status == 400
    fields = [e["field"] for e in body["errors"]]
    assert any("per1kInput" in f for f in fields)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
