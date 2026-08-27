"""Simulation_API Lambda handler for the Melbourne Agent Village.

Single Lambda behind an API Gateway HTTP API (payload format v2.0) that serves
the Visualisation_Client (a cross-origin SPA) and issues control commands to the
Simulation Engine via a DynamoDB CONTROL item.

Authoritative contract: DESIGN.md
  - §5  HTTP API surface (base path /v1, envelope {ok, data|error}).
  - §3  DynamoDB single-table `village` (PK=SIM#<simId>, SK prefixes, GSI1/GSI2).
  - §4  canonical JSON payload schemas.
Requirements: 2 (control), 13 (persistence reads), 14 (events/summary/decision
trail), 15.6/15.7 (agent & location detail), 16.6/16.10 (asset serving),
17.4/17.5 (auth before any read/write), 18.1/18.2/18.9 (config + cost report).

Routing is method + path based (no framework). Every response is JSON
{ok, data|error} with permissive CORS headers, except asset serving which issues
a 302 redirect to a presigned S3 URL.

AUTH (Req 17.4/17.5): the HTTP API uses a Cognito JWT authorizer, so validated
claims arrive at event.requestContext.authorizer.jwt.claims. The handler
confirms those claims are present BEFORE any DynamoDB read/write and returns 401
otherwise. Env ALLOW_ANON=1 is a documented local-testing escape hatch, default
OFF; it must never be set in a deployed stack.

Environment variables (set by the CDK stack, built separately):
  TABLE_NAME      DynamoDB table name (default "village")
  ASSETS_BUCKET   S3 bucket holding generated PNGs (for asset serving)
  ASSET_FN_NAME   Asset_Generator Lambda function name (async invoke)
  AWS_REGION      provided by Lambda runtime; region is ap-southeast-2
  ALLOW_ANON      "1" to bypass auth for LOCAL testing only (default off)
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoConfig

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TABLE_NAME = os.environ.get("TABLE_NAME", "village")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
ASSET_FN_NAME = os.environ.get("ASSET_FN_NAME", "")
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
ALLOW_ANON = os.environ.get("ALLOW_ANON", "") == "1"

SCHEMA_VERSION = 1
MELBOURNE_TZ = "Australia/Melbourne"

# Event-log query cap (Req 14.2).
MAX_EVENTS = 500

# Agent-detail recent-event count (Req 15.6).
AGENT_RECENT_EVENTS = 10

# Presigned URL TTL for asset serving (Req 16.6, well under the 2s budget).
PRESIGN_TTL_SECONDS = 300

# -- Control commands (Req 2) ------------------------------------------------
VALID_COMMANDS = ("start", "pause", "resume", "stop")

# -- Config validation ranges (Req 4 / Req 18.1, mirrored from the engine) ---
POPULATION_MIN, POPULATION_MAX, POPULATION_DEFAULT = 5, 100, 25
ACCEL_MIN, ACCEL_MAX, ACCEL_DEFAULT = 1, 60, 4
INVOCATIONS_MIN, INVOCATIONS_MAX = 1, 100_000
SPEND_MIN, SPEND_MAX = 1.00, 10_000.00
PRICE_MIN, PRICE_MAX = 0.00, 1000.00

# Event categories (DESIGN §4).
COST_CATEGORY = "model"

# Legal / employment status enumerations (Req 14.9 summary).
LEGAL_STATUSES = ("clear", "suspected", "charged", "detained")
EMPLOYMENT_STATUSES = ("employed", "unemployed", "suspended")

# --------------------------------------------------------------------------- #
# Lazy AWS clients (so unit tests can monkeypatch before first use)
# --------------------------------------------------------------------------- #

_clients: dict = {}


def _table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _clients["table"]


def _s3():
    if "s3" not in _clients:
        # Force the REGIONAL endpoint + virtual-host addressing for presigned
        # URLs. The default (global) endpoint `bucket.s3.amazonaws.com` answers a
        # cross-region bucket with a 307 redirect that carries NO CORS headers,
        # so the browser blocks the image fetch. `s3.<region>.amazonaws.com`
        # serves the object directly with CORS headers. SigV4 is required for
        # presigned URLs against regional endpoints.
        _clients["s3"] = boto3.client(
            "s3",
            region_name=REGION,
            endpoint_url=f"https://s3.{REGION}.amazonaws.com",
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
    return _clients["s3"]


def _lambda():
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda", region_name=REGION)
    return _clients["lambda"]


# --------------------------------------------------------------------------- #
# JSON encoding (Decimal -> int/float) & HTTP envelope
# --------------------------------------------------------------------------- #

class _DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as Decimal; emit ints as ints, else floats."""

    def default(self, o):
        if isinstance(o, Decimal):
            # Preserve integers as integers; everything else as float.
            if o == o.to_integral_value():
                return int(o)
            return float(o)
        return super().default(o)


CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Max-Age": "300",
}


def _resp(status: int, body: dict, extra_headers: dict | None = None):
    headers = dict(CORS_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _ok(data, status: int = 200):
    return _resp(status, {"ok": True, "data": data})


def _err(status: int, message: str, extra: dict | None = None):
    body = {"ok": False, "error": message}
    if extra:
        body.update(extra)
    return _resp(status, body)


# --------------------------------------------------------------------------- #
# DynamoDB access helpers (single-table `village`, PK=SIM#<simId>)
# --------------------------------------------------------------------------- #

def _pk(sim_id: str) -> str:
    return f"SIM#{sim_id}"


def _get_item(sim_id: str, sk: str):
    resp = _table().get_item(Key={"PK": _pk(sim_id), "SK": sk})
    return resp.get("Item")


def _query_prefix(sim_id: str, sk_prefix: str, limit: int | None = None):
    """All items under one PK whose SK begins with the prefix (paginated)."""
    items = []
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(_pk(sim_id)) & Key("SK").begins_with(sk_prefix),
    }
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        if limit is not None and len(items) >= limit:
            return items[:limit]
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _get_status(sim_id: str) -> dict:
    return _get_item(sim_id, "STATUS") or {}


def _get_config(sim_id: str) -> dict:
    return _get_item(sim_id, "CONFIG") or {}


def _list_agents(sim_id: str):
    return _query_prefix(sim_id, "AGENT#")


def _list_locations(sim_id: str):
    return _query_prefix(sim_id, "LOC#")


def _get_agent(sim_id: str, agent_id: str):
    return _get_item(sim_id, f"AGENT#{agent_id}")


def _get_location(sim_id: str, loc_id: str):
    return _get_item(sim_id, f"LOC#{loc_id}")


# --------------------------------------------------------------------------- #
# Time / location-status helpers (reimplemented from engine timeutil, Req 3)
# --------------------------------------------------------------------------- #

def _localize(sim_time_iso: str):
    """Parse an ISO-8601 Simulated_Time to a tz-aware datetime.

    Prefers the embedded offset in the string. Falls back to Australia/Melbourne
    via zoneinfo when the string is naive.
    """
    try:
        dt = datetime.fromisoformat(sim_time_iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.replace(tzinfo=ZoneInfo(MELBOURNE_TZ))
        except Exception:  # pragma: no cover - zoneinfo always available on 3.9+
            dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hhmm_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _is_open_at(loc: dict, sim_dt: datetime) -> bool:
    """True if within the location's opening hours for the current Melbourne day.

    hours is 7 entries indexed 0=Monday..6=Sunday (DESIGN §4). Overnight windows
    (close <= open) are handled. Mirrors engine timeutil.is_open_at (Req 3.4/3.10).
    """
    hours = loc.get("hours") or []
    idx = sim_dt.weekday()
    if idx >= len(hours):
        return False
    oh = hours[idx]
    try:
        open_min = _hhmm_minutes(oh["open"])
        close_min = _hhmm_minutes(oh["close"])
    except (KeyError, ValueError, TypeError):
        return False
    now_min = sim_dt.hour * 60 + sim_dt.minute
    if close_min <= open_min:
        # overnight (e.g. 20:00-02:00)
        return now_min >= open_min or now_min < close_min
    return open_min <= now_min < close_min


def _location_status(loc: dict, sim_dt: datetime | None, occupancy: int) -> str:
    """open | closed | at_capacity (Req 3.4/3.5/3.10)."""
    capacity = loc.get("capacity")
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        capacity = 0
    if sim_dt is None or not _is_open_at(loc, sim_dt):
        return "closed"
    if capacity and occupancy >= capacity:
        return "at_capacity"
    return "open"


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres."""
    import math

    r = 6371000.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _present_agents_by_location(agents: list, locations: list) -> dict:
    """Map locId -> [agent items present], present == within 25m of the loc.

    Prefers an explicit presentLocationId in agent state; otherwise falls back to
    a 25 m proximity test (Req 3.5 occupancy rule).
    """
    by_loc: dict[str, list] = {loc_id_of(l): [] for l in locations}
    loc_by_id = {loc_id_of(l): l for l in locations}
    for a in agents:
        state = a.get("state") or {}
        present = state.get("presentLocationId")
        if present and present in by_loc:
            by_loc[present].append(a)
            continue
        # proximity fallback
        alat, alon = state.get("lat"), state.get("lon")
        if alat is None or alon is None:
            continue
        for loc_id, loc in loc_by_id.items():
            if loc.get("lat") is None or loc.get("lon") is None:
                continue
            if _haversine_m(alat, alon, loc["lat"], loc["lon"]) <= 25.0:
                by_loc[loc_id].append(a)
                break
    return by_loc


def loc_id_of(loc: dict) -> str:
    return loc.get("id") or (loc.get("SK", "").split("#", 1)[-1])


def agent_id_of(a: dict) -> str:
    return a.get("id") or (a.get("SK", "").split("#", 1)[-1])


# --------------------------------------------------------------------------- #
# Auth (Req 17.4 / 17.5)
# --------------------------------------------------------------------------- #

def _claims(event: dict) -> dict | None:
    """Extract validated Cognito JWT claims from the HTTP API v2 request context.

    The JWT authorizer places validated claims at
    requestContext.authorizer.jwt.claims. Presence of a non-empty claims object
    is proof the token was validated by API Gateway (Req 17.5).
    """
    rc = (event or {}).get("requestContext") or {}
    authorizer = rc.get("authorizer") or {}
    jwt = authorizer.get("jwt") or {}
    claims = jwt.get("claims")
    if isinstance(claims, dict) and claims:
        return claims
    # Some deployments surface a flat authorizer.claims map.
    flat = authorizer.get("claims")
    if isinstance(flat, dict) and flat:
        return flat
    return None


def _authorized(event: dict) -> bool:
    """Validate caller identity BEFORE any read/write (Req 17.4)."""
    if ALLOW_ANON:  # documented local-only escape hatch, default off
        return True
    return _claims(event) is not None


# --------------------------------------------------------------------------- #
# Request parsing (API Gateway HTTP API payload v2.0)
# --------------------------------------------------------------------------- #

def _method(event: dict) -> str:
    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    return (http.get("method") or event.get("httpMethod") or "GET").upper()


def _raw_path(event: dict) -> str:
    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    return http.get("path") or event.get("rawPath") or event.get("path") or "/"


def _query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def _body(event: dict) -> dict:
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _strip_stage(path: str) -> str:
    """HTTP APIs with a $default stage don't prefix the stage; but a named stage
    would. We normalise so routing always sees a path beginning at /v1."""
    if path.startswith("/v1/") or path == "/v1":
        return path
    idx = path.find("/v1/")
    if idx >= 0:
        return path[idx:]
    if path.endswith("/v1"):
        return "/v1"
    return path


# --------------------------------------------------------------------------- #
# Route table: (METHOD, compiled-regex) -> handler(event, sim_id, **path_params)
# --------------------------------------------------------------------------- #

def _route_patterns():
    sim = r"(?P<simId>[^/]+)"
    return [
        ("POST", re.compile(rf"^/v1/sim/{sim}/control$"), _handle_control),
        ("GET", re.compile(rf"^/v1/sim/{sim}/summary$"), _handle_summary),
        ("GET", re.compile(rf"^/v1/sim/{sim}/state$"), _handle_state),
        ("GET", re.compile(rf"^/v1/sim/{sim}/agents/(?P<agentId>[^/]+)$"), _handle_agent_detail),
        ("GET", re.compile(rf"^/v1/sim/{sim}/locations$"), _handle_locations),
        ("GET", re.compile(rf"^/v1/sim/{sim}/locations/(?P<locId>[^/]+)$"), _handle_location_detail),
        ("GET", re.compile(rf"^/v1/sim/{sim}/events/decision-trail$"), _handle_decision_trail),
        ("GET", re.compile(rf"^/v1/sim/{sim}/events$"), _handle_events),
        ("GET", re.compile(rf"^/v1/sim/{sim}/cost$"), _handle_cost),
        ("GET", re.compile(rf"^/v1/sim/{sim}/assets/(?P<subjectId>[^/]+)$"), _handle_asset_get),
        ("POST", re.compile(rf"^/v1/sim/{sim}/config$"), _handle_config),
        ("POST", re.compile(rf"^/v1/sim/{sim}/assets/generate$"), _handle_asset_generate),
    ]


# --------------------------------------------------------------------------- #
# Endpoint handlers
# --------------------------------------------------------------------------- #

def _status_view(status_item: dict) -> dict:
    """Normalise a STATUS item into the SPA-facing shape."""
    return {
        "status": status_item.get("status", "stopped"),
        "simTime": status_item.get("simTime"),
        "accel": status_item.get("accel"),
        "updatedAt": status_item.get("updatedAt"),
    }


def _handle_control(event, sim_id, **_):
    """POST /v1/sim/{simId}/control — write a CONTROL item the engine consumes.

    Rejects unknown commands (Req 2.7 shape). Command legality vs current status
    is enforced authoritatively by the engine when it consumes the CONTROL item;
    the API rejects only structurally invalid commands and returns current STATUS.
    """
    body = _body(event)
    command = body.get("command")
    if command not in VALID_COMMANDS:
        status = _get_status(sim_id)
        return _err(
            400,
            f"invalid command '{command}'; expected one of {list(VALID_COMMANDS)}",
            {"data": {"status": _status_view(status), "accepted": False}},
        )

    nonce = uuid.uuid4().hex
    requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _table().put_item(
        Item={
            "PK": _pk(sim_id),
            "SK": "CONTROL",
            "schemaVersion": SCHEMA_VERSION,
            "command": command,
            "requestedAt": requested_at,
            "nonce": nonce,
        }
    )
    status = _get_status(sim_id)
    return _ok({
        "accepted": True,
        "command": command,
        "nonce": nonce,
        "requestedAt": requested_at,
        "status": _status_view(status),
    })


def _handle_summary(event, sim_id, **_):
    """GET /v1/sim/{simId}/summary — Req 14.9 aggregation for the current day."""
    status = _get_status(sim_id)
    sim_time = status.get("simTime")

    agents = _list_agents(sim_id)
    legal_counts = {s: 0 for s in LEGAL_STATUSES}
    employment_counts = {s: 0 for s in EMPLOYMENT_STATUSES}
    for a in agents:
        state = a.get("state") or {}
        ls = state.get("legalStatus")
        es = state.get("employmentStatus")
        if ls in legal_counts:
            legal_counts[ls] += 1
        if es in employment_counts:
            employment_counts[es] += 1

    # Day window: 00:00 of the current simulated day .. current simTime (Req 14.9).
    day_start = None
    if sim_time:
        dt = _localize(sim_time)
        if dt is not None:
            day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    crime_count = _count_events_in_window(sim_id, "crime", day_start, sim_time)
    conversation_count = _count_conversations_in_window(sim_id, day_start, sim_time)

    return _ok({
        "simTime": sim_time,
        "status": status.get("status", "stopped"),
        "legalStatusCounts": legal_counts,
        "employmentStatusCounts": employment_counts,
        "crimeCount": crime_count,
        "conversationCount": conversation_count,
    })


def _handle_state(event, sim_id, **_):
    """GET /v1/sim/{simId}/state — compact world snapshot for the SPA poll."""
    status = _get_status(sim_id)
    agents = _list_agents(sim_id)

    agent_views = []
    conversations = []
    seen_convos = set()
    for a in agents:
        state = a.get("state") or {}
        persona = a.get("persona") or {}
        action = state.get("currentAction") or {}
        agent_views.append({
            "id": agent_id_of(a),
            "name": persona.get("name"),
            "lat": state.get("lat"),
            "lon": state.get("lon"),
            "action": action.get("type"),
            "route": action.get("route"),
            "legal": state.get("legalStatus"),
            "employment": state.get("employmentStatus"),
        })
        # Derive live conversations from agents whose action is `socialise` and
        # who share a conversation grouping. The engine may also record co-located
        # participants on the action; we surface participants + location when present.
        convo = state.get("conversation")
        if convo and convo.get("participants"):
            key = tuple(sorted(convo["participants"]))
            if key not in seen_convos:
                seen_convos.add(key)
                conversations.append({
                    "participants": list(convo["participants"]),
                    "locationId": convo.get("locationId") or state.get("presentLocationId"),
                })

    return _ok({
        "simTime": status.get("simTime"),
        "status": status.get("status", "stopped"),
        "accel": status.get("accel"),
        "agents": agent_views,
        "conversations": conversations,
    })


def _handle_agent_detail(event, sim_id, agentId=None, **_):
    """GET /v1/sim/{simId}/agents/{agentId} — full persona+state + last 10 events."""
    agent = _get_agent(sim_id, agentId)
    if agent is None:
        return _err(404, f"unknown agent '{agentId}'")

    # Last 10 events for this agent, ascending, via GSI2 (agent+time) (Req 15.6).
    recent = _query_agent_events(sim_id, agentId, limit=AGENT_RECENT_EVENTS)

    return _ok({
        "id": agent_id_of(agent),
        "provenance": agent.get("provenance"),
        "persona": agent.get("persona"),
        "state": agent.get("state"),
        "recentEvents": recent,
    })


def _handle_locations(event, sim_id, **_):
    """GET /v1/sim/{simId}/locations — all locations w/ status + present agents."""
    status = _get_status(sim_id)
    sim_dt = _localize(status.get("simTime")) if status.get("simTime") else None
    locations = _list_locations(sim_id)
    agents = _list_agents(sim_id)
    present = _present_agents_by_location(agents, locations)

    out = []
    for loc in locations:
        lid = loc_id_of(loc)
        occupants = present.get(lid, [])
        out.append(_location_view(loc, sim_dt, occupants))
    return _ok({"locations": out})


def _handle_location_detail(event, sim_id, locId=None, **_):
    """GET /v1/sim/{simId}/locations/{locId} — detail (Req 15.7)."""
    loc = _get_location(sim_id, locId)
    if loc is None:
        return _err(404, f"unknown location '{locId}'")
    status = _get_status(sim_id)
    sim_dt = _localize(status.get("simTime")) if status.get("simTime") else None
    agents = _list_agents(sim_id)
    present = _present_agents_by_location(agents, [loc]).get(locId, [])
    return _ok(_location_view(loc, sim_dt, present, detail=True))


def _location_view(loc: dict, sim_dt, occupants: list, detail: bool = False) -> dict:
    view = {
        "id": loc_id_of(loc),
        "name": loc.get("name"),
        "category": loc.get("category"),
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
        "capacity": loc.get("capacity"),
        "status": _location_status(loc, sim_dt, len(occupants)),
        "occupancy": len(occupants),
        "presentAgents": [
            {"id": agent_id_of(a), "name": (a.get("persona") or {}).get("name")}
            for a in occupants
        ],
    }
    if detail:
        view["hours"] = loc.get("hours")
        view["isDetentionFacility"] = loc.get("isDetentionFacility", False)
        if loc.get("price") is not None:
            view["price"] = loc.get("price")
    return view


def _handle_events(event, sim_id, **_):
    """GET /v1/sim/{simId}/events — filtered event query (Req 14.2/14.3).

    Filters: category, agentId, fromSimTime, toSimTime, cursor. Returns <=500
    ascending entries + a `more` flag. Empty result (not error) when none match.
    Uses GSI1 (category) or GSI2 (agent) where a single-key filter is supplied;
    otherwise queries the base table by PK with begins_with(EVENT#).
    """
    q = _query(event)
    category = q.get("category")
    agent_id = q.get("agentId")
    from_st = q.get("fromSimTime")
    to_st = q.get("toSimTime")
    try:
        cursor = int(q.get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    cursor = max(0, cursor)

    entries = _load_events(sim_id, category=category, agent_id=agent_id,
                           from_st=from_st, to_st=to_st)
    # ascending by (simTime, seq); GSI SKs already sort this way, base table too.
    entries.sort(key=lambda e: (e.get("simTime") or "", int(e.get("seq") or 0)))

    window = entries[cursor:cursor + MAX_EVENTS]
    more = len(entries) > cursor + MAX_EVENTS
    return _ok({
        "events": [_event_view(e) for e in window],
        "more": more,
        "nextCursor": (cursor + MAX_EVENTS) if more else None,
        "count": len(window),
    })


def _handle_decision_trail(event, sim_id, **_):
    """GET /v1/sim/{simId}/events/decision-trail?actionEventSeq= (Req 14.4/14.5)."""
    q = _query(event)
    raw_seq = q.get("actionEventSeq")
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError):
        return _err(400, "actionEventSeq must be an integer")

    item = _get_item(sim_id, f"EVENT#{str(seq).zfill(20)}")
    if item is None or item.get("category") != "action":
        # Req 14.5: empty result + error indication; leave entries unchanged.
        return _err(404, f"no decision trail for action event seq {seq}",
                    {"data": None})

    detail = item.get("detail") or {}
    return _ok({
        "actionEventSeq": seq,
        "simTime": item.get("simTime"),
        "perceptionInput": detail.get("perceptionInput"),
        "retrievedMemoryIds": detail.get("retrievedMemoryIds", []),
        "action": detail.get("action"),
    })


def _handle_cost(event, sim_id, **_):
    """GET /v1/sim/{simId}/cost — cost report from `model` events (Req 18.9).

    Broken down by modelId and by purpose; estimated spend in USD to 2dp using
    the configured per-1000-token prices. Returns zeros (not an error) when no
    invocation record falls within the requested range.
    """
    q = _query(event)
    from_st = q.get("fromSimTime")
    to_st = q.get("toSimTime")

    prices = ((_get_config(sim_id).get("budget") or {}).get("prices")) or {}
    events = _load_events(sim_id, category=COST_CATEGORY, from_st=from_st, to_st=to_st)

    by_model: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    total_inv = 0
    total_in = 0
    total_out = 0
    total_spend = Decimal("0")

    for e in events:
        detail = e.get("detail") or {}
        model_id = detail.get("modelId") or "unknown"
        purpose = detail.get("purpose") or "unknown"
        in_tok = int(detail.get("inputTokens") or 0)
        out_tok = int(detail.get("outputTokens") or 0)
        spend = _estimate_spend(prices.get(model_id), in_tok, out_tok)

        total_inv += 1
        total_in += in_tok
        total_out += out_tok
        total_spend += spend

        _accumulate(by_model, model_id, in_tok, out_tok, spend)
        _accumulate(by_purpose, purpose, in_tok, out_tok, spend)

    return _ok({
        "fromSimTime": from_st,
        "toSimTime": to_st,
        "invocationCount": total_inv,
        "inputTokens": total_in,
        "outputTokens": total_out,
        "estimatedSpendUSD": _money(total_spend),
        "byModel": {k: _finalize_bucket(v) for k, v in by_model.items()},
        "byPurpose": {k: _finalize_bucket(v) for k, v in by_purpose.items()},
    })


def _handle_asset_get(event, sim_id, subjectId=None, **_):
    """GET /v1/sim/{simId}/assets/{subjectId} — 302 to a presigned S3 URL.

    404-style {ok:false} when no manifest entry exists or the object is missing
    (Req 16.6/16.10). The manifest is left unchanged in all cases.
    """
    manifest = _get_item(sim_id, f"ASSET#{subjectId}")
    if manifest is None:
        return _err(404, f"no image available for subject '{subjectId}'")

    key = manifest.get("imageKey")
    if not key:
        return _err(404, f"no image available for subject '{subjectId}'")

    if not ASSETS_BUCKET:
        return _err(500, "ASSETS_BUCKET is not configured")

    # Confirm the object exists so we never 302 to a broken URL (Req 16.10).
    try:
        _s3().head_object(Bucket=ASSETS_BUCKET, Key=key)
    except Exception:  # noqa: BLE001 - any error means "not retrievable"
        return _err(404, f"no image available for subject '{subjectId}'")

    try:
        url = _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": ASSETS_BUCKET, "Key": key},
            ExpiresIn=PRESIGN_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(500, f"could not sign asset url: {exc}")

    # 302 redirect (browser follows to the image bytes).
    return {
        "statusCode": 302,
        "headers": {
            **CORS_HEADERS,
            "Location": url,
            "Cache-Control": "no-store",
        },
        "body": "",
    }


def _handle_config(event, sim_id, **_):
    """POST /v1/sim/{simId}/config — validate + store Config incl. budget.

    Validates population (Req 4: 5..100) and budget (Req 18.1/18.2 ranges),
    persists the CONFIG item, and sets a seedPending flag the seed process
    consumes to generate the population (population generation is heavy and runs
    out-of-band). Returns the stored config on success; a 400 with the offending
    value + permitted range on any validation failure (Req 18.2).
    """
    body = _body(event)
    errors = _validate_config(body)
    if errors:
        return _err(400, "config validation failed", {"errors": errors})

    config = _normalize_config(sim_id, body)
    _table().put_item(Item=config)

    return _ok({
        "config": _config_view(config),
        "seedPending": True,
        "message": "config stored; population generation triggered via seedPending flag",
    })


def _handle_asset_generate(event, sim_id, **_):
    """POST /v1/sim/{simId}/assets/generate — async-invoke the Asset_Generator.

    body {subjectId?}: with subjectId -> regenerate that subject; without ->
    generate_all. Returns 202 accepted. Falls back to writing a trigger item if
    ASSET_FN_NAME is not configured.
    """
    body = _body(event)
    subject_id = body.get("subjectId")

    payload = {"simId": sim_id}
    if subject_id:
        payload["action"] = "regenerate"
        payload["subjectId"] = subject_id
    else:
        payload["action"] = "generate_all"

    if ASSET_FN_NAME:
        try:
            _lambda().invoke(
                FunctionName=ASSET_FN_NAME,
                InvocationType="Event",  # async
                Payload=json.dumps(payload).encode("utf-8"),
            )
            return _ok({"accepted": True, "mode": "async-invoke", "request": payload}, status=202)
        except Exception as exc:  # noqa: BLE001
            return _err(502, f"could not invoke asset generator: {exc}")

    # No function name configured: persist a trigger the generator can poll.
    requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _table().put_item(
        Item={
            "PK": _pk(sim_id),
            "SK": f"ASSETGEN#{uuid.uuid4().hex}",
            "schemaVersion": SCHEMA_VERSION,
            "action": payload["action"],
            "subjectId": subject_id,
            "requestedAt": requested_at,
        }
    )
    return _ok({"accepted": True, "mode": "trigger-item", "request": payload}, status=202)


# --------------------------------------------------------------------------- #
# Event loading (GSI1 category / GSI2 agent / base-table PK scan)
# --------------------------------------------------------------------------- #

def _load_events(sim_id, category=None, agent_id=None, from_st=None, to_st=None):
    """Return event items matching the supplied filters (in-Python range filter).

    Query strategy:
      - agent_id supplied  -> GSI2 (GSI2PK = SIM#<id>#AGENT#<agentId>)
      - category supplied  -> GSI1 (GSI1PK = SIM#<id>#CAT#<category>)
      - otherwise          -> base table PK query, SK begins_with EVENT#
    When both category and agent_id are supplied we query GSI2 (agent) and filter
    category in Python, since agent partitions are smaller.
    """
    if agent_id:
        items = _query_gsi2(sim_id, agent_id)
        if category:
            items = [e for e in items if e.get("category") == category]
    elif category:
        items = _query_gsi1(sim_id, category)
    else:
        items = _query_prefix(sim_id, "EVENT#")

    def in_range(e):
        st = e.get("simTime")
        if from_st is not None and (st is None or st < from_st):
            return False
        if to_st is not None and (st is None or st > to_st):
            return False
        return True

    return [e for e in items if in_range(e)]


def _query_gsi1(sim_id, category):
    items = []
    kwargs = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(f"{_pk(sim_id)}#CAT#{category}"),
    }
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _query_gsi2(sim_id, agent_id):
    items = []
    kwargs = {
        "IndexName": "GSI2",
        "KeyConditionExpression": Key("GSI2PK").eq(f"{_pk(sim_id)}#AGENT#{agent_id}"),
    }
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _query_agent_events(sim_id, agent_id, limit=10):
    """Last `limit` events for an agent, ascending, via GSI2."""
    items = _query_gsi2(sim_id, agent_id)
    items.sort(key=lambda e: (e.get("simTime") or "", int(e.get("seq") or 0)))
    tail = items[-limit:] if limit else items
    return [_event_view(e) for e in tail]


def _count_events_in_window(sim_id, category, from_st, to_st):
    return len(_load_events(sim_id, category=category, from_st=from_st, to_st=to_st))


def _count_conversations_in_window(sim_id, from_st, to_st):
    """Conversation count == conversation-ended entries in the window (Req 14.9)."""
    events = _load_events(sim_id, category="conversation", from_st=from_st, to_st=to_st)
    count = 0
    for e in events:
        detail = e.get("detail") or {}
        if detail.get("kind") == "conversation-ended" \
                or "conversation-ended" in (e.get("description") or ""):
            count += 1
    # If the engine writes exactly one conversation entry per conversation with
    # no explicit marker, fall back to counting all conversation entries.
    if count == 0 and events:
        count = len(events)
    return count


def _event_view(e: dict) -> dict:
    return {
        "seq": int(e["seq"]) if e.get("seq") is not None else None,
        "simTime": e.get("simTime"),
        "realTime": e.get("realTime"),
        "category": e.get("category"),
        "agents": e.get("agents") or [],
        "locationId": e.get("locationId"),
        "description": e.get("description"),
        "detail": e.get("detail"),
    }


# --------------------------------------------------------------------------- #
# Cost math helpers (Req 18.9)
# --------------------------------------------------------------------------- #

def _estimate_spend(price: dict | None, in_tok: int, out_tok: int) -> Decimal:
    """USD spend for one invocation given a {per1kInput, per1kOutput} price."""
    if not price:
        return Decimal("0")
    per_in = Decimal(str(price.get("per1kInput", 0) or 0))
    per_out = Decimal(str(price.get("per1kOutput", 0) or 0))
    cost = (Decimal(in_tok) / Decimal(1000)) * per_in \
        + (Decimal(out_tok) / Decimal(1000)) * per_out
    return cost


def _money(x: Decimal) -> float:
    """Round a Decimal to 2dp USD and return a float."""
    from decimal import ROUND_HALF_UP

    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _accumulate(bucket: dict, key: str, in_tok: int, out_tok: int, spend: Decimal):
    b = bucket.setdefault(key, {"invocationCount": 0, "inputTokens": 0,
                                "outputTokens": 0, "_spend": Decimal("0")})
    b["invocationCount"] += 1
    b["inputTokens"] += in_tok
    b["outputTokens"] += out_tok
    b["_spend"] += spend


def _finalize_bucket(b: dict) -> dict:
    return {
        "invocationCount": b["invocationCount"],
        "inputTokens": b["inputTokens"],
        "outputTokens": b["outputTokens"],
        "estimatedSpendUSD": _money(b["_spend"]),
    }


# --------------------------------------------------------------------------- #
# Config validation & normalisation (Req 4 population, Req 18.1/18.2 budget)
# --------------------------------------------------------------------------- #

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_config(body: dict) -> list:
    """Return a list of validation error dicts; empty list == valid."""
    errors = []

    # population (Req 4.2/4.4): 5..100, default 25 when omitted.
    pop = body.get("population", POPULATION_DEFAULT)
    if not (isinstance(pop, int) and not isinstance(pop, bool)
            and POPULATION_MIN <= pop <= POPULATION_MAX):
        errors.append({
            "field": "population",
            "value": pop,
            "permitted": f"integer {POPULATION_MIN}..{POPULATION_MAX}",
        })

    # acceleration factor (Req 1 / glossary): 1..60, default 4.
    accel = body.get("accelerationFactor", ACCEL_DEFAULT)
    if not (isinstance(accel, int) and not isinstance(accel, bool)
            and ACCEL_MIN <= accel <= ACCEL_MAX):
        errors.append({
            "field": "accelerationFactor",
            "value": accel,
            "permitted": f"integer {ACCEL_MIN}..{ACCEL_MAX}",
        })

    # budget (Req 18.1/18.2).
    budget = body.get("budget")
    if not isinstance(budget, dict):
        errors.append({"field": "budget", "value": budget, "permitted": "object required"})
        return errors

    inv = budget.get("maxInvocationsPerSimHour")
    if not (isinstance(inv, int) and not isinstance(inv, bool)
            and INVOCATIONS_MIN <= inv <= INVOCATIONS_MAX):
        errors.append({
            "field": "budget.maxInvocationsPerSimHour",
            "value": inv,
            "permitted": f"integer {INVOCATIONS_MIN}..{INVOCATIONS_MAX}",
        })

    spend = budget.get("maxSpendUSD")
    if not (_is_number(spend) and SPEND_MIN <= spend <= SPEND_MAX):
        errors.append({
            "field": "budget.maxSpendUSD",
            "value": spend,
            "permitted": f"{SPEND_MIN}..{SPEND_MAX} USD",
        })

    prices = budget.get("prices")
    if not isinstance(prices, dict) or not prices:
        errors.append({
            "field": "budget.prices",
            "value": prices,
            "permitted": "non-empty object keyed by modelId with per1kInput/per1kOutput",
        })
    else:
        # A price must be present for every model the sim uses. If the caller
        # supplies modelsUsed, require each; otherwise validate the supplied ones.
        models_used = body.get("modelsUsed") or list(prices.keys())
        for mid in models_used:
            price = prices.get(mid)
            if not isinstance(price, dict):
                errors.append({
                    "field": f"budget.prices.{mid}",
                    "value": price,
                    "permitted": f"object with per1kInput/per1kOutput each {PRICE_MIN}..{PRICE_MAX} USD",
                })
                continue
            for label in ("per1kInput", "per1kOutput"):
                val = price.get(label)
                if not (_is_number(val) and PRICE_MIN <= val <= PRICE_MAX):
                    errors.append({
                        "field": f"budget.prices.{mid}.{label}",
                        "value": val,
                        "permitted": f"{PRICE_MIN}..{PRICE_MAX} USD",
                    })

    return errors


def _to_decimal(v):
    """Convert numeric leaves to Decimal for DynamoDB (recursively)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        return {k: _to_decimal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_decimal(x) for x in v]
    return v


def _normalize_config(sim_id: str, body: dict) -> dict:
    """Build the CONFIG item to persist (numbers as Decimal for DynamoDB)."""
    item = {
        "PK": _pk(sim_id),
        "SK": "CONFIG",
        "schemaVersion": SCHEMA_VERSION,
        "simId": sim_id,
        "status": "stopped",
        "population": body.get("population", POPULATION_DEFAULT),
        "accelerationFactor": body.get("accelerationFactor", ACCEL_DEFAULT),
        "seedPending": True,
    }
    # Copy through recognised config fields (DESIGN §4 Config schema).
    for field in ("startSimTime", "timezone", "detentionFacilityId", "artStyleClause",
                  "decayRates", "energyRecoveryRate", "initialNeeds", "budget"):
        if field in body:
            item[field] = _to_decimal(body[field])
    return item


def _config_view(item: dict) -> dict:
    """Echo the stored config without the internal PK/SK keys."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


# --------------------------------------------------------------------------- #
# Lambda entrypoint
# --------------------------------------------------------------------------- #

def handler(event, context=None):
    """API Gateway HTTP API (v2.0) proxy entrypoint."""
    event = event or {}
    method = _method(event)

    # CORS preflight — no auth, no data access.
    if method == "OPTIONS":
        return _resp(204, {"ok": True, "data": None})

    path = _strip_stage(_raw_path(event))

    # AUTH GATE (Req 17.4/17.5): validate identity BEFORE any read/write.
    if not _authorized(event):
        return _err(401, "unauthorized: valid caller identity required")

    for m, pattern, fn in _route_patterns():
        if m != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        params = match.groupdict()
        sim_id = params.pop("simId", None)
        if not sim_id:
            return _err(400, "simId is required")
        try:
            return fn(event, sim_id, **params)
        except Exception as exc:  # noqa: BLE001 - surface as 500 envelope
            return _err(500, f"internal error: {exc}")

    return _err(404, f"no route for {method} {path}")
