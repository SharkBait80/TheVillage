"""World delete + re-seed for the Melbourne Agent Village.

Runs inside the Asset_Generator Lambda (which already has DynamoDB RW + Bedrock
InvokeModel and a 15-minute timeout). A reseed:

  1. Deletes the existing world for the sim (agents, locations, jobs, events,
     conversations, relationships, injected events, asset manifests/failures,
     status/control) under PK = SIM#<simId>.
  2. Regenerates personas with UNIQUE, LLM-generated biographies + personality
     traits per agent (Bedrock Claude in-region), consistent with each agent's
     assigned Myers-Briggs type. Falls back to deterministic templates if the
     model is unavailable so a reseed never fails outright.
  3. Writes the fresh world items to DynamoDB (status = stopped).
  4. Kicks off portrait/artwork generation (unique per subject) via generate_all.

Seed source data (locations.json, jobs.json, config.json) and the persona
generator (generate_personas.py) are bundled alongside this module in the
Lambda package by the CDK build.
"""

import json
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

HERE = os.path.dirname(os.path.abspath(__file__))

TABLE_NAME = os.environ.get("TABLE_NAME", "village")
TABLE_REGION = os.environ.get("TABLE_REGION", "ap-southeast-2")
# Fast Claude model in-region for short biography generation. Overridable.
TEXT_MODEL_ID = os.environ.get(
    "TEXT_MODEL_ID",
    "au.anthropic.claude-haiku-4-5-20251001-v1:0",
)

_clients = {}


def _table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource(
            "dynamodb", region_name=TABLE_REGION
        ).Table(TABLE_NAME)
    return _clients["table"]


def _bedrock_text():
    if "bedrock_text" not in _clients:
        _clients["bedrock_text"] = boto3.client(
            "bedrock-runtime", region_name=TABLE_REGION
        )
    return _clients["bedrock_text"]


def _pk(sim_id):
    return f"SIM#{sim_id}"


def _load_json(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 1. Delete the existing world
# --------------------------------------------------------------------------- #

def delete_world(sim_id):
    """Delete every item under PK = SIM#<simId>. Returns the number deleted."""
    table = _table()
    deleted = 0
    kwargs = {"KeyConditionExpression": Key("PK").eq(_pk(sim_id)),
              "ProjectionExpression": "PK, SK"}
    keys = []
    while True:
        resp = table.query(**kwargs)
        keys.extend((it["PK"], it["SK"]) for it in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    with table.batch_writer() as batch:
        for pk, sk in keys:
            batch.delete_item(Key={"PK": pk, "SK": sk})
            deleted += 1
    return deleted


# --------------------------------------------------------------------------- #
# 2. LLM biography + personality enrichment (unique per agent)
# --------------------------------------------------------------------------- #

_MBTI_HINT = {
    "E": "outgoing and energised by people",
    "I": "reflective and content with solitude",
    "S": "practical and grounded in the here-and-now",
    "N": "imaginative and drawn to ideas and possibilities",
    "T": "logical and decisive",
    "F": "warm and led by values and empathy",
    "J": "organised and likes plans settled",
    "P": "spontaneous and keeps options open",
}


def _mbti_description(mbti):
    mbti = (mbti or "").upper()
    if len(mbti) < 4:
        return "a distinctive personality"
    return "; ".join(_MBTI_HINT[c] for c in mbti[:4] if c in _MBTI_HINT)


def make_llm_enricher(sim_id):
    """Return an `enrich(persona)->{background, traits}` callable backed by
    Bedrock. Each call produces a UNIQUE short biography and 3-5 personality
    traits consistent with the agent's MBTI. Returns None on any failure so the
    caller falls back to the deterministic template.
    """
    def enrich(p):
        name = p.get("name", "")
        age = p.get("age", "")
        occupation = p.get("occupation", "")
        mbti = (p.get("mbti") or "").upper()
        persona_desc = _mbti_description(mbti)
        prompt = (
            "You are writing a short character bio for a cozy storybook "
            "simulation set in Melbourne, Australia. Write for this resident:\n"
            f"- Name: {name}\n- Age: {age}\n- Occupation: {occupation}\n"
            f"- Personality ({mbti}): {persona_desc}\n\n"
            "Return STRICT JSON only, no prose, with exactly these keys:\n"
            '{"background": "<2-3 sentence unique first-or-third person bio, '
            'under 500 characters, reflecting their personality and life in '
            'Melbourne>", "traits": ["<3 to 5 short lowercase adjective traits '
            'consistent with their personality>"]}'
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 400,
            "temperature": 0.9,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = _bedrock_text().invoke_model(
            modelId=TEXT_MODEL_ID,
            body=json.dumps(body),
            accept="application/json",
            contentType="application/json",
        )
        payload = json.loads(resp["body"].read())
        # Anthropic Messages API on Bedrock: content is a list of blocks.
        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        text = text.strip()
        # Extract the JSON object (model may wrap it in ```json fences).
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        out = {}
        if isinstance(data.get("background"), str):
            out["background"] = data["background"].strip()
        if isinstance(data.get("traits"), list):
            out["traits"] = data["traits"]
        return out or None

    return enrich


# --------------------------------------------------------------------------- #
# 3. Build + write items (mirrors seed.py build_items / _to_dynamo)
# --------------------------------------------------------------------------- #

def _to_dynamo(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _assign_jobs(agents, jobs):
    jobs_by_occ = {}
    for j in jobs:
        j["assignedAgentId"] = None
        jobs_by_occ.setdefault(j["occupation"], []).append(j)
    for a in agents:
        if a["state"]["employmentStatus"] != "employed":
            continue
        occ = a["persona"]["occupation"]
        free = next((j for j in jobs_by_occ.get(occ, [])
                     if j["assignedAgentId"] is None), None)
        if free is None:
            a["state"]["jobId"] = None
            continue
        free["assignedAgentId"] = a["id"]
        a["state"]["jobId"] = free["id"]


def _build_items(sim_id, config, locations, agents, jobs):
    pk = _pk(sim_id)
    import time as _time
    reseeded_at = str(int(_time.time() * 1000))  # ms epoch — bumps each reseed
    items = [
        {"PK": pk, "SK": "CONFIG", **config},
        {"PK": pk, "SK": "STATUS", "status": "stopped",
         "simTime": config["startSimTime"], "accel": config["accelerationFactor"],
         "reseededAt": reseeded_at, "schemaVersion": 1},
    ]
    for loc in locations:
        items.append({"PK": pk, "SK": f"LOC#{loc['id']}", **loc})
    for a in agents:
        items.append({"PK": pk, "SK": f"AGENT#{a['id']}", **a})
    for j in jobs:
        items.append({"PK": pk, "SK": f"JOB#{j['id']}", **j})
    for a in agents:
        for r in a.get("relationships", []):
            items.append({"PK": pk, "SK": f"REL#{r['from']}#{r['to']}", **r})
    return items


def _write(items):
    table = _table()
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=_to_dynamo(item))


# --------------------------------------------------------------------------- #
# 4. Orchestration
# --------------------------------------------------------------------------- #

def reseed(sim_id, population=None, use_llm=True, generate_assets=True):
    """Delete + re-seed the world. Returns a summary dict.

    Portraits/artwork are generated per subject with a deterministic seed, so
    every agent gets a unique portrait; biographies + traits are LLM-generated
    per agent when `use_llm` is set (falls back to templates on model failure).

    Biographies are generated CONCURRENTLY with a bounded thread pool so a large
    population (e.g. 500 agents) completes well within the Lambda timeout; the
    world is written to DynamoDB BEFORE portrait generation so a slow/timed-out
    image phase never leaves the world empty.
    """
    from concurrent.futures import ThreadPoolExecutor
    from generate_personas import generate_personas  # bundled alongside

    config = _load_json("config.json")
    locations = _load_json("locations.json")["locations"]
    jobs = _load_json("jobs.json")["jobs"]
    pop = population if population is not None else config.get("population", 25)

    deleted = delete_world(sim_id)

    llm_bios = False
    if use_llm:
        # First pass: build personas WITHOUT LLM (fast, deterministic).
        agents = generate_personas(pop, locations, seed=42, enrich=None)
        # Second pass: enrich bios concurrently, keyed by agent id.
        enricher = make_llm_enricher(sim_id)

        def _enrich_one(agent):
            p = agent["persona"]
            try:
                extra = enricher({
                    "name": p["name"], "age": p["age"],
                    "occupation": p["occupation"], "mbti": p.get("mbti", ""),
                    "traits": p.get("traits", []),
                }) or {}
            except Exception:  # noqa: BLE001
                return
            bio = str(extra.get("background") or "").strip()
            if bio:
                p["background"] = bio[:1000]
            new_traits = extra.get("traits")
            if isinstance(new_traits, list):
                cleaned = [str(t).strip()[:40] for t in new_traits if str(t).strip()]
                if 3 <= len(cleaned) <= 6:
                    p["traits"] = cleaned

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(_enrich_one, agents))
        llm_bios = True
    else:
        agents = generate_personas(pop, locations, seed=42, enrich=None)

    _assign_jobs(agents, jobs)

    items = _build_items(sim_id, config, locations, agents, jobs)
    _write(items)  # persist the world BEFORE the slow image phase

    assets_summary = None
    if generate_assets:
        # Portrait/artwork generation for a large population is heavy; run it in
        # a SEPARATE async Lambda invocation with its own timeout budget so the
        # reseed returns immediately with the world (and LLM bios) already
        # written. Portraits then fill in progressively. Falls back to inline
        # generation only if self-invocation isn't possible.
        fn_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        if fn_name:
            try:
                boto3.client("lambda", region_name=TABLE_REGION).invoke(
                    FunctionName=fn_name,
                    InvocationType="Event",
                    Payload=json.dumps({"action": "generate_all", "simId": sim_id}).encode("utf-8"),
                )
                assets_summary = {"mode": "async-invoke", "status": "started"}
            except Exception as exc:  # noqa: BLE001
                assets_summary = {"error": f"async asset invoke failed: {exc}"}
        else:
            try:
                import index as asset_index  # the Lambda's own module
                assets_summary = asset_index.generate_all(sim_id)
            except Exception as exc:  # noqa: BLE001 — reseed still succeeded
                assets_summary = {"error": str(exc)}

    return {
        "simId": sim_id,
        "deleted": deleted,
        "agents": len(agents),
        "locations": len(locations),
        "jobs": len(jobs),
        "population": pop,
        "llmBios": llm_bios,
        "assets": assets_summary,
    }
