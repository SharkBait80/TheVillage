"""Melbourne Agent Village — Agent Harness (Bedrock AgentCore Runtime container).

Implements the AgentCore Runtime container contract (see
`.orchestrator/research/agentcore.md` §1):
  - GET  /ping          -> {"status": "healthy"}  (HTTP 200 health check)
  - POST /invocations    -> receives the JSON payload defined in DESIGN.md §6 and
                            returns the structured op-specific response.

The engine (already built, 129 tests passing) calls
`invoke_agent_runtime(agentRuntimeArn, runtimeSessionId=<agentId+simDay>,
payload=json.dumps(req))` with the request `req` documented in DESIGN §6 and
validates the response per Requirement 6.5. Response shapes MUST match exactly:

  op=decision  -> {"action":{"type","targetType","targetId","expectedDurationMin","crimeType"?},"reasoning":"<=280 chars"}
  op=plan       -> {"plan":[{"type","targetType","targetId"} x3..12]}
  op=reflect    -> {"reflections":[{"text","sourceMemoryIds":[..1..20]} x1..5]}
  op=utterance  -> {"utterance":"<=500 chars"}

Every response additionally carries:
  {"tokenUsage":{"inputTokens","outputTokens","modelId","purpose"}}   (DESIGN §6 / Req18.3)
and, if any AgentCore Memory op degraded, {"memoryDegraded": true}      (Req 7.7).

Model calls use the Bedrock Runtime Messages API with
anthropic_version="bedrock-2023-05-31" and enforce max_tokens. Throttling is
retried up to 5 times with exponential backoff (1s -> 30s) + 0..50% jitter
(Req 18.7). Memory reads/writes go to AgentCore Memory (env MEMORY_ID) and are
fully guarded so a Memory failure never fails a decision (Req 7.7/7.8).

Run the built-in self-test with:  python3 harness.py --selftest
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

# --------------------------------------------------------------------------- #
# Constants — models, region, contract (DESIGN §1 / §6).
# --------------------------------------------------------------------------- #
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
REASONING_MODEL = "au.anthropic.claude-opus-5"
FAST_MODEL = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
ANTHROPIC_VERSION = "bedrock-2023-05-31"

MEMORY_ID = os.environ.get("MEMORY_ID")  # AgentCore Memory resource id (may be None)

# Action / crime type enums (DESIGN §4 / Req 6.4 / Req 11.1).
ACTION_TYPES = {
    "sleep", "eat", "work", "travel", "socialise",
    "shop", "leisure", "commit_crime", "idle",
}
CRIME_TYPES = {"theft", "burglary", "vandalism", "fraud"}
TARGET_TYPES = {"location", "agent"}

DURATION_MIN = 1
DURATION_MAX = 600
PLAN_MIN, PLAN_MAX = 3, 12
REFLECT_MIN, REFLECT_MAX = 1, 5
SRC_MEM_MIN, SRC_MEM_MAX = 1, 20
UTTERANCE_MAX_CHARS = 500

# max_tokens ceilings per op (bounded output; keeps cost predictable).
MAX_TOKENS = {
    "decision": 512,
    "plan": 1024,
    "reflect": 1500,
    "utterance": 400,
}

# Purpose labels the engine's Budget_Accountant expects (budget.VALID_PURPOSES).
PURPOSE_BY_OP = {
    "decision": "decision_cycle",
    "plan": "decision_cycle",
    "reflect": "reflection",
    "utterance": "conversation",
}

# Bedrock throttling retry (Req 18.7).
RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_DELAY = 1.0
RETRY_MAX_DELAY = 30.0

# Memory op guarding (Req 7.7 / 7.8).
MEMORY_TIMEOUT_S = 5.0
MEMORY_MAX_RETRIES = 2  # up to 2 retries after the first attempt
STM_LIST_CAP = 50
LTM_TOPK = 10


# --------------------------------------------------------------------------- #
# Lazy AWS clients (so --selftest / imports work without credentials).
# --------------------------------------------------------------------------- #
_bedrock_client = None
_memory_client = None


def get_bedrock_client():
    """Bedrock Runtime data-plane client (invoke_model)."""
    global _bedrock_client
    if _bedrock_client is None:
        import boto3  # deferred import
        _bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock_client


def get_memory_client():
    """AgentCore data-plane client (create_event / retrieve_memory_records / list_events)."""
    global _memory_client
    if _memory_client is None:
        import boto3  # deferred import
        _memory_client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _memory_client


# --------------------------------------------------------------------------- #
# Throttling retry (Req 18.7).
# --------------------------------------------------------------------------- #
def _is_throttling(exc: Exception) -> bool:
    name = exc.__class__.__name__
    if name in ("ThrottlingException", "TooManyRequestsException",
                "ServiceQuotaExceededException", "ModelTimeoutException"):
        return True
    # botocore ClientError carries an error code in response metadata.
    code = getattr(exc, "response", {}) or {}
    err = code.get("Error", {}) if isinstance(code, dict) else {}
    return err.get("Code") in (
        "ThrottlingException", "TooManyRequestsException", "Throttling",
        "ProvisionedThroughputExceededException", "ServiceUnavailableException",
    )


def _retry_delays(rng: random.Random):
    """Yield backoff delays 1s doubling to 30s max + 0..50% jitter (Req 18.7)."""
    delay = RETRY_INITIAL_DELAY
    for _ in range(RETRY_MAX_ATTEMPTS):
        base = min(RETRY_MAX_DELAY, delay)
        yield base + rng.uniform(0.0, 0.5 * base)
        delay = min(RETRY_MAX_DELAY, delay * 2)


def invoke_with_retry(fn: Callable[[], Any],
                      sleep: Callable[[float], None] = time.sleep,
                      rng: Optional[random.Random] = None) -> Any:
    """Call fn(), retrying up to 5 times on Bedrock throttling (Req 18.7/18.8)."""
    rng = rng or random.Random()
    delays = list(_retry_delays(rng))
    last_exc: Optional[Exception] = None
    for attempt in range(RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_throttling(exc) or attempt >= RETRY_MAX_ATTEMPTS:
                raise
            last_exc = exc
            sleep(delays[attempt])
    if last_exc:
        raise last_exc


# --------------------------------------------------------------------------- #
# Bedrock Messages API call.
# --------------------------------------------------------------------------- #
def call_bedrock(model_id: str, system: str, user_text: str, max_tokens: int,
                 client=None, sleep=time.sleep,
                 rng: Optional[random.Random] = None) -> Tuple[str, int, int]:
    """Invoke a Claude model via the Bedrock Messages API.

    Returns (text, input_tokens, output_tokens). Throttling is retried (Req 18.7).
    """
    client = client or get_bedrock_client()
    body = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }

    def _do():
        return client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body).encode("utf-8"),
        )

    resp = invoke_with_retry(_do, sleep=sleep, rng=rng)
    raw = resp["body"].read()
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    data = json.loads(raw)

    text = _extract_text(data)
    usage = data.get("usage", {}) or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    return text, in_tok, out_tok


def _extract_text(data: Dict[str, Any]) -> str:
    """Concatenate text blocks from a Claude Messages API response body."""
    parts = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
# Robust JSON parsing of model output (Req 6: retry parse once, safe fallback).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object out of a model's text response.

    Tolerates surrounding prose / markdown fences. Returns None if unparseable.
    """
    if not text:
        return None
    stripped = text.strip()
    # Strip markdown code fences if present.
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    # First try: direct parse.
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Second try: extract the first {...} span.
    m = _JSON_OBJ_RE.search(stripped)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Prompt builders (well-engineered, JSON-mode style).
# --------------------------------------------------------------------------- #
def _persona_block(persona: Dict[str, Any]) -> str:
    traits = ", ".join(persona.get("traits", []) or [])
    return (
        f"Name: {persona.get('name','?')}\n"
        f"Age: {persona.get('age','?')}\n"
        f"Occupation: {persona.get('occupation','?')}\n"
        f"Traits: {traits}\n"
        f"Background: {persona.get('background','')}\n"
        f"Home location: {persona.get('homeLocationId','?')}\n"
        f"Wake time: {persona.get('wakeTime','?')}"
    )


def _state_block(state: Dict[str, Any]) -> str:
    needs = state.get("needs", {}) or {}
    crit = state.get("critical", {}) or {}
    critical_flags = [k for k, v in crit.items() if v]
    return (
        f"Position: ({state.get('lat')},{state.get('lon')}) "
        f"at location {state.get('presentLocationId','?')}\n"
        f"Needs (0-100): hunger={needs.get('hunger')}, energy={needs.get('energy')}, "
        f"social={needs.get('social')}, fun={needs.get('fun')}\n"
        f"Critical needs (<20): {critical_flags or 'none'}\n"
        f"Cash: {state.get('cash')} AUD\n"
        f"Employment: {state.get('employmentStatus','?')}  "
        f"Legal: {state.get('legalStatus','?')}\n"
        f"Job: {state.get('jobId') or 'none'}  "
        f"Daily living cost: {state.get('dailyLivingCost')} AUD"
    )


def _dayplan_block(state: Dict[str, Any]) -> str:
    """Render the agent's intended day plan so decisions follow through on it."""
    plan = state.get("dayPlan") or []
    if not plan:
        return "(no plan yet — act sensibly for the time of day)"
    lines = []
    for i, step in enumerate(plan[:12]):
        if isinstance(step, dict):
            lines.append(
                f"{i+1}. {step.get('type','?')} -> {step.get('targetType','location')}:"
                f"{step.get('targetId','?')}"
            )
        else:
            lines.append(f"{i+1}. {step}")
    return "\n".join(lines)


def _reachable_block(reachable: List[Dict[str, Any]]) -> str:
    lines = []
    for loc in (reachable or [])[:20]:
        lines.append(
            f"- id={loc.get('id')} name=\"{loc.get('name')}\" "
            f"category={loc.get('category')} "
            f"remainingCapacity={loc.get('remainingCapacity')} "
            f"travelMin={loc.get('travelMin')}"
        )
    return "\n".join(lines) if lines else "(none)"


def _colocated_block(colocated: List[Dict[str, Any]]) -> str:
    lines = []
    for a in (colocated or [])[:10]:
        lines.append(f"- id={a.get('id')} name=\"{a.get('name')}\" "
                     f"action={a.get('actionType')}")
    return "\n".join(lines) if lines else "(none)"


def _memory_block(records: List[Any], cap: int) -> str:
    lines = []
    for r in (records or [])[:cap]:
        if isinstance(r, dict):
            txt = r.get("text") or r.get("description") or r.get("content") or json.dumps(r)
            mid = r.get("id") or r.get("memoryRecordId") or r.get("seq")
            lines.append(f"- [{mid}] {txt}" if mid is not None else f"- {txt}")
        else:
            lines.append(f"- {r}")
    return "\n".join(lines) if lines else "(none)"


def _perception_block(flags: Dict[str, Any], price_table: Dict[str, Any]) -> str:
    parts = [f"Financial pressure: {flags.get('financialPressure', 'normal')}"]
    if flags.get("criticalNeeds"):
        parts.append(f"Critical needs: {flags['criticalNeeds']}")
    if flags.get("pendingInvestigation"):
        parts.append(f"Pending investigation: {json.dumps(flags['pendingInvestigation'])}")
    if flags.get("rejectedPurchase"):
        parts.append("A recent purchase was rejected (insufficient funds).")
    if flags.get("employmentOffers"):
        parts.append(f"Nearby job offers: {json.dumps(flags['employmentOffers'])}")
    if price_table:
        parts.append(f"Price table (action -> AUD): {json.dumps(price_table)}")
    return "\n".join(parts)


def build_decision_prompt(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Build (system, user) prompts for a decision cycle. Req 6.3/6.4."""
    persona = payload.get("persona", {}) or {}
    state = payload.get("state", {}) or {}
    cur = payload.get("currentLocation", {}) or {}
    reachable = payload.get("reachable", []) or []
    colocated = payload.get("coLocated", []) or []
    stm = payload.get("shortTermMemory", []) or []
    ltm = payload.get("longTermMemory", []) or []
    flags = payload.get("perceptionFlags", {}) or {}
    price_table = payload.get("priceTable", {}) or {}
    failed = payload.get("failedValidation")

    system = (
        "You are a single inhabitant of a simulated Melbourne (the 'Agent Village'). "
        "You decide your own next action from what you perceive and remember. "
        "You are a living person with a life to get on with: follow your day plan, "
        "pursue your job, feed and enjoy yourself, and seek out others. Prefer a "
        "concrete, purposeful action (travel, work, eat, shop, leisure, socialise) "
        "over idling. Only choose \"idle\" when nothing else makes sense (e.g. it is "
        "the middle of the night and you are not sleeping). If it is night and your "
        "energy is not high, choose \"sleep\". "
        "You MUST reply with ONLY a single JSON object and no other text, in the form:\n"
        '{"action":{"type":"<one of sleep|eat|work|travel|socialise|shop|leisure|'
        'commit_crime|idle>","targetType":"<location|agent>","targetId":"<id>",'
        '"expectedDurationMin":<integer 1-600>,"crimeType":"<theft|burglary|vandalism|fraud>"},'
        '"reasoning":"<one short sentence, <=280 chars, explaining WHY you chose this>"}\n'
        "Rules: targetId MUST be your current location, one of the reachable locations, "
        "or one of the co-located agents. To socialise, set targetType=agent and "
        "targetId to a co-located agent id. To work/eat/shop/leisure, travel to a "
        "suitable location first if you are not already there. Only include "
        "\"crimeType\" when type is \"commit_crime\". If detained you may only "
        "sleep/eat/socialise/idle. Advance your DAY PLAN where it is sensible."
    )

    user = (
        f"Current simulated time: {payload.get('simTime','?')}\n\n"
        f"== YOUR PERSONA ==\n{_persona_block(persona)}\n\n"
        f"== YOUR STATE ==\n{_state_block(state)}\n\n"
        f"== YOUR DAY PLAN ==\n{_dayplan_block(state)}\n\n"
        f"== CURRENT LOCATION ==\n"
        f"id={cur.get('id')} name=\"{cur.get('name')}\" category={cur.get('category')}\n\n"
        f"== CO-LOCATED AGENTS (targetable for socialise/commit_crime) ==\n"
        f"{_colocated_block(colocated)}\n\n"
        f"== REACHABLE LOCATIONS (<=20, nearest first) ==\n"
        f"{_reachable_block(reachable)}\n\n"
        f"== PERCEPTION ==\n{_perception_block(flags, price_table)}\n\n"
        f"== SHORT-TERM MEMORY (today) ==\n{_memory_block(stm, 50)}\n\n"
        f"== LONG-TERM MEMORY ==\n{_memory_block(ltm, 20)}\n"
    )
    if failed:
        user += (
            f"\n== PREVIOUS ATTEMPT REJECTED ==\n"
            f"Your last action was rejected because: {failed}\n"
            f"Return a DIFFERENT valid action.\n"
        )
    user += "\nReturn ONLY the JSON object."
    return system, user


def build_plan_prompt(payload: Dict[str, Any]) -> Tuple[str, str]:
    persona = payload.get("persona", {}) or {}
    state = payload.get("state", {}) or {}
    reachable = payload.get("reachable", []) or []
    ltm = payload.get("longTermMemory", []) or []
    system = (
        "You are an inhabitant of a simulated Melbourne planning your day. "
        f"Produce a day plan of between {PLAN_MIN} and {PLAN_MAX} intended actions. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"plan":[{"type":"<action type>","targetType":"<location|agent>",'
        '"targetId":"<id>"}, ...]}\n'
        "Each type is one of sleep|eat|work|travel|socialise|shop|leisure|commit_crime|idle. "
        "Prefer targets from your reachable locations or known agents."
    )
    user = (
        f"Current simulated time: {payload.get('simTime','?')}\n\n"
        f"== YOUR PERSONA ==\n{_persona_block(persona)}\n\n"
        f"== YOUR STATE ==\n{_state_block(state)}\n\n"
        f"== REACHABLE LOCATIONS ==\n{_reachable_block(reachable)}\n\n"
        f"== LONG-TERM MEMORY ==\n{_memory_block(ltm, 20)}\n\n"
        f"Return ONLY the JSON object with {PLAN_MIN}-{PLAN_MAX} plan items."
    )
    return system, user


def build_reflect_prompt(payload: Dict[str, Any]) -> Tuple[str, str]:
    persona = payload.get("persona", {}) or {}
    stm = payload.get("shortTermMemory", []) or []
    ltm = payload.get("longTermMemory", []) or []
    system = (
        "You are an inhabitant of a simulated Melbourne reflecting at the end of a day. "
        f"Produce between {REFLECT_MIN} and {REFLECT_MAX} durable reflections drawn from "
        "your memories. Reply with ONLY a JSON object of the form:\n"
        '{"reflections":[{"text":"<insight, 1-500 chars>",'
        '"sourceMemoryIds":[<ids of memories this draws on, 1-20 ints>]}, ...]}\n'
        "Each reflection must cite the memory ids it is based on."
    )
    user = (
        f"Current simulated time: {payload.get('simTime','?')}\n\n"
        f"== YOUR PERSONA ==\n{_persona_block(persona)}\n\n"
        f"== TODAY'S MEMORIES (short-term; note the [id] of each) ==\n"
        f"{_memory_block(stm, 50)}\n\n"
        f"== EXISTING LONG-TERM MEMORY ==\n{_memory_block(ltm, 20)}\n\n"
        f"Return ONLY the JSON object with {REFLECT_MIN}-{REFLECT_MAX} reflections."
    )
    return system, user


def build_utterance_prompt(payload: Dict[str, Any]) -> Tuple[str, str]:
    persona = payload.get("persona", {}) or {}
    conv = payload.get("conversation", {}) or {}
    ltm = payload.get("longTermMemory", []) or []
    participants = conv.get("participants", []) or []
    utterances = conv.get("utterancesSoFar", []) or []
    system = (
        "You are an inhabitant of a simulated Melbourne speaking in a live conversation. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"utterance":"<what you say next, at most 500 characters>"}\n'
        "Speak in character, briefly and naturally, considering your relationships and memories."
    )
    rel_lines = []
    for p in participants:
        if isinstance(p, dict):
            rel_lines.append(
                f"- {p.get('name', p.get('id'))}: "
                f"familiarity={p.get('familiarity', 0)} sentiment={p.get('sentiment', 0)}"
            )
        else:
            rel_lines.append(f"- {p}")
    conv_lines = []
    for u in utterances:
        if isinstance(u, dict):
            conv_lines.append(f"{u.get('speaker', u.get('agentId', '?'))}: {u.get('text', u)}")
        else:
            conv_lines.append(str(u))
    user = (
        f"== YOUR PERSONA ==\n{_persona_block(persona)}\n\n"
        f"== PARTICIPANTS & YOUR RELATIONSHIPS ==\n"
        f"{chr(10).join(rel_lines) if rel_lines else '(none)'}\n\n"
        f"== RELEVANT MEMORIES ==\n{_memory_block(ltm, 10)}\n\n"
        f"== CONVERSATION SO FAR ==\n"
        f"{chr(10).join(conv_lines) if conv_lines else '(you speak first)'}\n\n"
        f"Return ONLY the JSON object with your next utterance (<=500 chars)."
    )
    return system, user


# --------------------------------------------------------------------------- #
# Response coercion / validation helpers (make the response schema-valid).
# --------------------------------------------------------------------------- #
def safe_idle_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback safe idle action (Req 6.7)."""
    state = payload.get("state", {}) or {}
    cur = payload.get("currentLocation", {}) or {}
    target_id = cur.get("id") or state.get("presentLocationId") or "unknown"
    return {
        "type": "idle",
        "targetType": "location",
        "targetId": target_id,
        "expectedDurationMin": 10,
    }


def coerce_action(obj: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a parsed model object into a schema-valid action.

    Falls back to a safe idle action for anything invalid. The engine performs
    authoritative validation (Req 6.5); here we only guarantee shape/enum sanity.
    """
    if not isinstance(obj, dict):
        return safe_idle_action(payload)
    action = obj.get("action", obj)  # tolerate either {"action":{..}} or {..}
    if not isinstance(action, dict):
        return safe_idle_action(payload)

    a_type = action.get("type")
    if a_type not in ACTION_TYPES:
        return safe_idle_action(payload)

    target_type = action.get("targetType")
    if target_type not in TARGET_TYPES:
        target_type = "location"

    target_id = action.get("targetId")
    if not isinstance(target_id, str) or not target_id:
        fallback = safe_idle_action(payload)
        return fallback

    try:
        dur = int(action.get("expectedDurationMin"))
    except (TypeError, ValueError):
        dur = 10
    dur = max(DURATION_MIN, min(DURATION_MAX, dur))

    result: Dict[str, Any] = {
        "type": a_type,
        "targetType": target_type,
        "targetId": target_id,
        "expectedDurationMin": dur,
    }
    if a_type == "commit_crime":
        crime = action.get("crimeType")
        if crime not in CRIME_TYPES:
            crime = "theft"
        result["crimeType"] = crime
    return result


REASONING_MAX_CHARS = 280


def coerce_reasoning(obj: Optional[Dict[str, Any]]) -> str:
    """Extract a short, sanitized reasoning string from a parsed decision object.

    Tolerates {"reasoning": "..."} at the top level; returns "" when absent or
    not a usable string. Trims to REASONING_MAX_CHARS (Req: human-readable
    thought process for the SPA).
    """
    if not isinstance(obj, dict):
        return ""
    r = obj.get("reasoning")
    if not isinstance(r, str):
        return ""
    return r.strip()[:REASONING_MAX_CHARS]


def coerce_plan(obj: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Coerce a parsed object into a 3..12 item plan, or None if not enough valid items."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("plan")
    if not isinstance(raw, list):
        return None
    items: List[Dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        tt = it.get("targetType")
        tid = it.get("targetId")
        if t in ACTION_TYPES and isinstance(tid, str) and tid:
            items.append({
                "type": t,
                "targetType": tt if tt in TARGET_TYPES else "location",
                "targetId": tid,
            })
    if len(items) < PLAN_MIN:
        return None
    return items[:PLAN_MAX]


def coerce_reflections(obj: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Coerce into 1..5 reflections each with 1..20 int sourceMemoryIds, or None."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("reflections")
    if not isinstance(raw, list):
        return None
    out: List[Dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        text = it.get("text")
        ids = it.get("sourceMemoryIds")
        if not isinstance(text, str) or not text.strip():
            continue
        clean_ids: List[int] = []
        if isinstance(ids, list):
            for x in ids:
                try:
                    clean_ids.append(int(x))
                except (TypeError, ValueError):
                    continue
        clean_ids = clean_ids[:SRC_MEM_MAX]
        if not clean_ids:
            continue  # a reflection must cite 1..20 source ids (Req 7.5)
        out.append({"text": text.strip()[:500], "sourceMemoryIds": clean_ids})
    if not out:
        return None
    return out[:REFLECT_MAX]


def coerce_utterance(obj: Optional[Dict[str, Any]], raw_text: str) -> str:
    """Coerce into an utterance string <=500 chars."""
    text = ""
    if isinstance(obj, dict) and isinstance(obj.get("utterance"), str):
        text = obj["utterance"]
    elif raw_text:
        text = raw_text
    return text.strip()[:UTTERANCE_MAX_CHARS]


# --------------------------------------------------------------------------- #
# Model-call-with-parse-retry (Req 6: retry parse once, fall back safely).
# --------------------------------------------------------------------------- #
def _sim_date(sim_time: Optional[str]) -> str:
    """Derive the STM session id 'day-<simDate>' from simTime (DESIGN §6)."""
    if isinstance(sim_time, str) and len(sim_time) >= 10:
        return f"day-{sim_time[:10]}"
    return "day-unknown"


def _call_and_parse(model_id: str, system: str, user: str, max_tokens: int,
                    bedrock=None, sleep=time.sleep,
                    rng: Optional[random.Random] = None
                    ) -> Tuple[Optional[Dict[str, Any]], str, int, int]:
    """Call the model, parse JSON; on parse failure re-ask ONCE more strictly.

    Returns (parsed_or_None, raw_text, total_input_tokens, total_output_tokens).
    """
    text, in_tok, out_tok = call_bedrock(model_id, system, user, max_tokens,
                                          client=bedrock, sleep=sleep, rng=rng)
    parsed = parse_json_object(text)
    if parsed is not None:
        return parsed, text, in_tok, out_tok
    # Retry parse once: re-ask with a stricter instruction.
    strict_user = user + (
        "\n\nYour previous reply could not be parsed as JSON. "
        "Reply with ONLY a single valid JSON object, no prose, no code fences."
    )
    text2, in2, out2 = call_bedrock(model_id, system, strict_user, max_tokens,
                                    client=bedrock, sleep=sleep, rng=rng)
    parsed2 = parse_json_object(text2)
    return parsed2, text2, in_tok + in2, out_tok + out2


# --------------------------------------------------------------------------- #
# AgentCore Memory ops — fully guarded (Req 7.7 / 7.8).
# --------------------------------------------------------------------------- #
class MemoryResult:
    """Aggregates memory outcomes so a failure never fails the decision."""

    def __init__(self):
        self.degraded = False
        self.stm: List[Any] = []
        self.ltm: List[Any] = []


def _guarded(fn: Callable[[], Any], mem: MemoryResult) -> Optional[Any]:
    """Run a memory op with up to 2 retries; set degraded flag on failure."""
    last_exc: Optional[Exception] = None
    for _ in range(MEMORY_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc is not None:
        mem.degraded = True
    return None


def memory_read(payload: Dict[str, Any], mem: MemoryResult, memory_client=None) -> None:
    """Read STM (list_events for the day) + LTM (semantic retrieve). Guarded."""
    if not MEMORY_ID:
        return  # no durable store configured; engine-supplied memory used instead
    client = memory_client or get_memory_client()
    agent_id = payload.get("agentId", "")
    sim_id = payload.get("simId", "")
    session_id = _sim_date(payload.get("simTime"))
    namespace = f"village/{agent_id}/semantic"

    def _list():
        return client.list_events(
            memoryId=MEMORY_ID, actorId=agent_id, sessionId=session_id,
            includePayloads=True,
        )

    def _retrieve():
        return client.retrieve_memory_records(
            memoryId=MEMORY_ID, namespace=namespace,
            searchCriteria={
                "searchQuery": f"plans, relationships and events relevant to agent {agent_id}",
                "topK": LTM_TOPK,
            },
        )

    listed = _guarded(_list, mem)
    if isinstance(listed, dict):
        mem.stm = (listed.get("events", []) or [])[:STM_LIST_CAP]
    retrieved = _guarded(_retrieve, mem)
    if isinstance(retrieved, dict):
        mem.ltm = (retrieved.get("memoryRecordSummaries", []) or [])[:LTM_TOPK]


def memory_write_event(payload: Dict[str, Any], text: str, mem: MemoryResult,
                       memory_client=None) -> None:
    """Write one short-term event to AgentCore Memory. Guarded (Req 7.8)."""
    if not MEMORY_ID or not text:
        return
    client = memory_client or get_memory_client()
    agent_id = payload.get("agentId", "")
    session_id = _sim_date(payload.get("simTime"))

    def _create():
        return client.create_event(
            memoryId=MEMORY_ID,
            actorId=agent_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{
                "conversational": {
                    "role": "ASSISTANT",
                    "content": {"text": text[:2000]},
                }
            }],
        )

    _guarded(_create, mem)


def merge_memory(payload: Dict[str, Any], mem: MemoryResult) -> Tuple[List[Any], List[Any]]:
    """Prefer AgentCore Memory; fall back to engine-supplied memory in the payload."""
    stm = mem.stm if mem.stm else (payload.get("shortTermMemory", []) or [])
    ltm = mem.ltm if mem.ltm else (payload.get("longTermMemory", []) or [])
    return stm, ltm


# --------------------------------------------------------------------------- #
# Op handlers.
# --------------------------------------------------------------------------- #
def _token_usage(model_id: str, purpose: str, in_tok: int, out_tok: int) -> Dict[str, Any]:
    return {
        "inputTokens": int(in_tok),
        "outputTokens": int(out_tok),
        "modelId": model_id,
        "purpose": purpose,
    }


def handle_decision(payload: Dict[str, Any], bedrock=None, memory_client=None,
                    sleep=time.sleep, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    mem = MemoryResult()
    memory_read(payload, mem, memory_client=memory_client)
    stm, ltm = merge_memory(payload, mem)
    enriched = dict(payload)
    enriched["shortTermMemory"] = stm
    enriched["longTermMemory"] = ltm

    system, user = build_decision_prompt(enriched)
    parsed, _raw, in_tok, out_tok = _call_and_parse(
        REASONING_MODEL, system, user, MAX_TOKENS["decision"],
        bedrock=bedrock, sleep=sleep, rng=rng)
    action = coerce_action(parsed, enriched)
    reasoning = coerce_reasoning(parsed)

    # Persist the decision as a short-term event (durable store).
    memory_write_event(
        payload,
        f"Decided action {action['type']} -> {action['targetId']} "
        f"for {action['expectedDurationMin']} min.",
        mem, memory_client=memory_client)

    resp: Dict[str, Any] = {
        "action": action,
        "reasoning": reasoning,
        "tokenUsage": _token_usage(REASONING_MODEL, "decision_cycle", in_tok, out_tok),
    }
    if mem.degraded:
        resp["memoryDegraded"] = True
    return resp


def handle_plan(payload: Dict[str, Any], bedrock=None, memory_client=None,
                sleep=time.sleep, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    mem = MemoryResult()
    memory_read(payload, mem, memory_client=memory_client)
    stm, ltm = merge_memory(payload, mem)
    enriched = dict(payload)
    enriched["shortTermMemory"] = stm
    enriched["longTermMemory"] = ltm

    system, user = build_plan_prompt(enriched)
    parsed, _raw, in_tok, out_tok = _call_and_parse(
        REASONING_MODEL, system, user, MAX_TOKENS["plan"],
        bedrock=bedrock, sleep=sleep, rng=rng)
    plan = coerce_plan(parsed)

    resp: Dict[str, Any] = {
        "tokenUsage": _token_usage(REASONING_MODEL, "decision_cycle", in_tok, out_tok),
    }
    if plan is None:
        # Signal planning failure per Req 6.11 (engine discards & logs).
        resp["plan"] = []
        resp["planFailed"] = True
    else:
        resp["plan"] = plan
        memory_write_event(
            payload,
            f"Planned day with {len(plan)} intended actions.",
            mem, memory_client=memory_client)
    if mem.degraded:
        resp["memoryDegraded"] = True
    return resp


def handle_reflect(payload: Dict[str, Any], bedrock=None, memory_client=None,
                   sleep=time.sleep, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    mem = MemoryResult()
    memory_read(payload, mem, memory_client=memory_client)
    stm, ltm = merge_memory(payload, mem)
    enriched = dict(payload)
    enriched["shortTermMemory"] = stm
    enriched["longTermMemory"] = ltm

    system, user = build_reflect_prompt(enriched)
    parsed, _raw, in_tok, out_tok = _call_and_parse(
        REASONING_MODEL, system, user, MAX_TOKENS["reflect"],
        bedrock=bedrock, sleep=sleep, rng=rng)
    reflections = coerce_reflections(parsed)

    resp: Dict[str, Any] = {
        "tokenUsage": _token_usage(REASONING_MODEL, "reflection", in_tok, out_tok),
    }
    if reflections is None:
        resp["reflections"] = []
        resp["reflectFailed"] = True
    else:
        resp["reflections"] = reflections
        # Feed reflections into long-term memory (durable store).
        for r in reflections:
            memory_write_event(payload, f"Reflection: {r['text']}", mem,
                               memory_client=memory_client)
    if mem.degraded:
        resp["memoryDegraded"] = True
    return resp


def handle_utterance(payload: Dict[str, Any], bedrock=None, memory_client=None,
                     sleep=time.sleep, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    mem = MemoryResult()
    memory_read(payload, mem, memory_client=memory_client)
    _stm, ltm = merge_memory(payload, mem)
    enriched = dict(payload)
    enriched["longTermMemory"] = ltm

    system, user = build_utterance_prompt(enriched)
    text, in_tok, out_tok = call_bedrock(
        FAST_MODEL, system, user, MAX_TOKENS["utterance"],
        client=bedrock, sleep=sleep, rng=rng)
    parsed = parse_json_object(text)
    utterance = coerce_utterance(parsed, text)

    resp: Dict[str, Any] = {
        "utterance": utterance,
        "tokenUsage": _token_usage(FAST_MODEL, "conversation", in_tok, out_tok),
    }
    if mem.degraded:
        resp["memoryDegraded"] = True
    return resp


OP_HANDLERS = {
    "decision": handle_decision,
    "plan": handle_plan,
    "reflect": handle_reflect,
    "utterance": handle_utterance,
}


def dispatch(payload: Dict[str, Any], bedrock=None, memory_client=None,
             sleep=time.sleep, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Route a request payload to its op handler (DESIGN §6)."""
    op = payload.get("op")
    handler = OP_HANDLERS.get(op)
    if handler is None:
        return {"error": f"unknown op '{op}'",
                "validOps": sorted(OP_HANDLERS.keys())}
    return handler(payload, bedrock=bedrock, memory_client=memory_client,
                   sleep=sleep, rng=rng)


# --------------------------------------------------------------------------- #
# Starlette app — AgentCore container contract.
# --------------------------------------------------------------------------- #
async def ping(request):
    return JSONResponse({"status": "healthy"})


async def invocations(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    session_id = request.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id")
    try:
        result = dispatch(payload)
    except Exception as exc:  # noqa: BLE001 — never crash the runtime; degrade safely.
        # For a decision op we can still return a safe idle action.
        if payload.get("op") == "decision":
            return JSONResponse({
                "action": safe_idle_action(payload),
                "tokenUsage": _token_usage(REASONING_MODEL, "decision_cycle", 0, 0),
                "error": f"{exc.__class__.__name__}: {exc}",
            })
        return JSONResponse({"error": f"{exc.__class__.__name__}: {exc}"},
                            status_code=500)
    if session_id:
        result["_sessionId"] = session_id
    return JSONResponse(result)


app = Starlette(routes=[
    Route("/ping", ping, methods=["GET"]),
    Route("/invocations", invocations, methods=["POST"]),
])


# --------------------------------------------------------------------------- #
# Self-test (python3 harness.py --selftest) — no AWS, no network.
# --------------------------------------------------------------------------- #
def _run_selftest() -> int:
    import sys

    failures: List[str] = []

    def check(cond: bool, msg: str):
        if not cond:
            failures.append(msg)

    # --- Stubbed Bedrock client returning canned Claude Messages bodies. ---
    class _FakeBody:
        def __init__(self, s: str):
            self._s = s.encode("utf-8")

        def read(self):
            return self._s

    class FakeBedrock:
        """Returns a canned response body based on which model is called."""

        def __init__(self):
            self.calls = []

        def _canned_for(self, model_id: str) -> Dict[str, Any]:
            if model_id == FAST_MODEL:
                text = json.dumps({"utterance": "G'day! Lovely morning at the cafe."})
            else:
                # Opus 5 — infer op from a marker we can't see, so return an
                # object valid for decision/plan/reflect by including all keys;
                # handlers pick the key they need.
                text = json.dumps({
                    "action": {"type": "eat", "targetType": "location",
                               "targetId": "loc_cafe", "expectedDurationMin": 30},
                    "plan": [
                        {"type": "eat", "targetType": "location", "targetId": "loc_cafe"},
                        {"type": "work", "targetType": "location", "targetId": "loc_work"},
                        {"type": "travel", "targetType": "location", "targetId": "loc_home"},
                    ],
                    "reflections": [
                        {"text": "I enjoy mornings at the cafe.", "sourceMemoryIds": [1, 2]},
                    ],
                })
            return {
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 123, "output_tokens": 45},
            }

        def invoke_model(self, modelId=None, contentType=None, accept=None, body=None):
            self.calls.append(modelId)
            canned = self._canned_for(modelId)
            return {"body": _FakeBody(json.dumps(canned))}

    fake = FakeBedrock()
    fast_rng = random.Random(0)

    # --- Fake payload (DESIGN §6 decision request). ---
    base_payload = {
        "op": "decision",
        "simId": "melb",
        "agentId": "agent_01",
        "simTime": "2026-03-02T08:15:00+11:00",
        "persona": {
            "name": "Aroha Ngata", "age": 34, "occupation": "Barista",
            "traits": ["warm", "impulsive", "curious"],
            "background": "A Melbourne barista who loves people.",
            "homeLocationId": "loc_home", "wakeTime": "07:00",
        },
        "state": {
            "lat": -37.81, "lon": 144.96, "presentLocationId": "loc_home",
            "needs": {"hunger": 15, "energy": 68, "social": 70, "fun": 66},
            "critical": {"hunger": True, "energy": False,
                         "social": False, "fun": False},
            "cash": 230.0, "employmentStatus": "employed",
            "legalStatus": "clear", "jobId": "job_barista_1",
            "dailyLivingCost": 40.0,
        },
        "currentLocation": {"id": "loc_home", "name": "Home",
                            "category": "residence"},
        "coLocated": [{"id": "agent_02", "name": "Bob", "actionType": "idle"}],
        "reachable": [
            {"id": "loc_cafe", "name": "Corner Cafe", "category": "food",
             "hours": [], "remainingCapacity": 40, "travelMin": 5},
            {"id": "loc_work", "name": "The Roastery", "category": "workplace",
             "hours": [], "remainingCapacity": 10, "travelMin": 8},
        ],
        "shortTermMemory": [{"id": 1, "text": "Woke up hungry."}],
        "longTermMemory": [{"id": 2, "text": "I love the cafe."}],
        "perceptionFlags": {"criticalNeeds": ["hunger"],
                            "financialPressure": "normal"},
        "priceTable": {"eat": 12.5, "shop": 25.0},
        "failedValidation": None,
    }

    # --- 1. Prompt builders work and include key context. ---
    sys_p, usr_p = build_decision_prompt(base_payload)
    check("JSON object" in sys_p, "decision system prompt missing JSON instruction")
    check("Aroha Ngata" in usr_p, "decision prompt missing persona name")
    check("Corner Cafe" in usr_p, "decision prompt missing reachable location")
    check("hunger" in usr_p, "decision prompt missing needs")
    check("critical" in usr_p.lower(), "decision prompt missing critical flags")

    # replacement path includes failedValidation.
    repl = dict(base_payload)
    repl["failedValidation"] = "target closed"
    _s, u2 = build_decision_prompt(repl)
    check("target closed" in u2, "replacement prompt missing failedValidation")

    # --- 2. JSON parsing helpers (fenced, prose-wrapped, plain). ---
    check(parse_json_object('{"a":1}') == {"a": 1}, "plain JSON parse failed")
    check(parse_json_object('```json\n{"a":1}\n```') == {"a": 1},
          "fenced JSON parse failed")
    check(parse_json_object('Here you go: {"a":1} thanks') == {"a": 1},
          "prose-wrapped JSON parse failed")
    check(parse_json_object("not json at all") is None,
          "unparseable text should return None")

    # --- 3. Each op returns a schema-valid response with tokenUsage. ---
    # decision
    dresp = handle_decision(base_payload, bedrock=fake,
                            sleep=lambda s: None, rng=fast_rng)
    a = dresp.get("action", {})
    check(a.get("type") in ACTION_TYPES, "decision: bad action type")
    check(a.get("targetType") in TARGET_TYPES, "decision: bad targetType")
    check(isinstance(a.get("targetId"), str) and a["targetId"],
          "decision: bad targetId")
    check(isinstance(a.get("expectedDurationMin"), int)
          and DURATION_MIN <= a["expectedDurationMin"] <= DURATION_MAX,
          "decision: bad duration")
    tu = dresp.get("tokenUsage", {})
    check(tu.get("inputTokens") == 123 and tu.get("outputTokens") == 45,
          "decision: tokenUsage not populated from usage")
    check(tu.get("modelId") == REASONING_MODEL, "decision: wrong modelId")
    check(tu.get("purpose") == "decision_cycle", "decision: wrong purpose")

    # plan
    pp = dict(base_payload); pp["op"] = "plan"
    presp = handle_plan(pp, bedrock=fake, sleep=lambda s: None, rng=fast_rng)
    plan = presp.get("plan")
    check(isinstance(plan, list) and PLAN_MIN <= len(plan) <= PLAN_MAX,
          "plan: not 3..12 items")
    check(all(x.get("type") in ACTION_TYPES for x in plan),
          "plan: invalid action type in items")
    check(presp["tokenUsage"]["purpose"] == "decision_cycle",
          "plan: wrong purpose")
    check(presp["tokenUsage"]["inputTokens"] == 123, "plan: tokenUsage missing")

    # reflect
    rp = dict(base_payload); rp["op"] = "reflect"
    rresp = handle_reflect(rp, bedrock=fake, sleep=lambda s: None, rng=fast_rng)
    refs = rresp.get("reflections")
    check(isinstance(refs, list) and REFLECT_MIN <= len(refs) <= REFLECT_MAX,
          "reflect: not 1..5 reflections")
    check(all(isinstance(r.get("text"), str) and r["text"] for r in refs),
          "reflect: missing text")
    check(all(1 <= len(r.get("sourceMemoryIds", [])) <= SRC_MEM_MAX for r in refs),
          "reflect: sourceMemoryIds not 1..20")
    check(rresp["tokenUsage"]["purpose"] == "reflection", "reflect: wrong purpose")

    # utterance (fast model)
    up = dict(base_payload); up["op"] = "utterance"
    up["conversation"] = {
        "participants": [{"id": "agent_02", "name": "Bob",
                          "familiarity": 20, "sentiment": 10}],
        "utterancesSoFar": [{"speaker": "Bob", "text": "Morning!"}],
    }
    uresp = handle_utterance(up, bedrock=fake, sleep=lambda s: None, rng=fast_rng)
    utt = uresp.get("utterance")
    check(isinstance(utt, str) and 0 < len(utt) <= UTTERANCE_MAX_CHARS,
          "utterance: bad string")
    check(uresp["tokenUsage"]["modelId"] == FAST_MODEL,
          "utterance: must use fast model")
    check(uresp["tokenUsage"]["purpose"] == "conversation",
          "utterance: wrong purpose")
    check(FAST_MODEL in fake.calls, "utterance: fast model was not invoked")

    # --- 4. Fallback: unparseable model output -> safe idle action. ---
    class BadBedrock:
        def invoke_model(self, **kw):
            body = json.dumps({
                "content": [{"type": "text", "text": "sorry, no JSON here"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
            return {"body": _FakeBody(body)}

    dbad = handle_decision(base_payload, bedrock=BadBedrock(),
                           sleep=lambda s: None, rng=random.Random(1))
    check(dbad["action"]["type"] == "idle",
          "fallback: unparseable output should yield idle action")
    check(dbad["action"]["targetId"] == "loc_home",
          "fallback: idle should target current location")
    # two parse attempts => tokens summed from both calls.
    check(dbad["tokenUsage"]["inputTokens"] == 20,
          "fallback: token usage should sum both parse attempts")

    # --- 5. Throttling retry backoff schedule (Req 18.7). ---
    delays = list(_retry_delays(random.Random(42)))
    check(len(delays) == RETRY_MAX_ATTEMPTS, "retry: wrong number of delays")
    check(1.0 <= delays[0] <= 1.5, "retry: first delay not 1s + <=50% jitter")
    check(delays[-1] <= RETRY_MAX_DELAY * 1.5,
          "retry: last delay exceeds 30s + jitter cap")

    class ThrottleThenOk:
        def __init__(self):
            self.n = 0

        def invoke_model(self, **kw):
            self.n += 1
            if self.n < 3:
                raise type("ThrottlingException", (Exception,), {})()
            body = json.dumps({
                "content": [{"type": "text", "text": json.dumps({
                    "action": {"type": "idle", "targetType": "location",
                               "targetId": "loc_home", "expectedDurationMin": 10}})}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return {"body": _FakeBody(body)}

    tob = ThrottleThenOk()
    dth = handle_decision(base_payload, bedrock=tob,
                          sleep=lambda s: None, rng=random.Random(3))
    check(tob.n == 3, "retry: should have retried through throttling")
    check(dth["action"]["type"] == "idle", "retry: action after retry invalid")

    # --- 6. Memory guard: a failing memory client never fails the op and
    #        sets memoryDegraded (Req 7.7). ---
    import harness as _self  # to toggle module-level MEMORY_ID
    old_mem_id = _self.MEMORY_ID
    _self.MEMORY_ID = "VillageMemory-abc1234567"
    try:
        class BadMemory:
            def list_events(self, **kw):
                raise RuntimeError("memory down")

            def retrieve_memory_records(self, **kw):
                raise RuntimeError("memory down")

            def create_event(self, **kw):
                raise RuntimeError("memory down")

        dmem = _self.handle_decision(base_payload, bedrock=fake,
                                     memory_client=BadMemory(),
                                     sleep=lambda s: None, rng=random.Random(4))
        check(dmem.get("memoryDegraded") is True,
              "memory: degraded flag not set on failure")
        check(dmem["action"]["type"] in ACTION_TYPES,
              "memory: decision must still succeed on memory failure")
    finally:
        _self.MEMORY_ID = old_mem_id

    # --- 7. dispatch routes and rejects unknown op. ---
    check("action" in dispatch(base_payload, bedrock=fake,
                               sleep=lambda s: None, rng=random.Random(5)),
          "dispatch: decision did not route")
    unknown = dispatch({"op": "bogus"}, bedrock=fake)
    check("error" in unknown, "dispatch: unknown op should return error")

    # --- Report. ---
    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELFTEST PASSED: all checks green")
    print(f"  ops verified: decision, plan, reflect, utterance")
    print(f"  bedrock models invoked: {sorted(set(fake.calls))}")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_run_selftest())
    # Local dev server (production uses: uvicorn harness:app --host 0.0.0.0 --port 8080).
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
