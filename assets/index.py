"""Asset_Generator Lambda for Melbourne Agent Village (Requirement 16).

Generates one portrait per Agent and one artwork per Location for a Simulation
using Stable Diffusion on Amazon Bedrock, stores the PNG to S3, and records one
Asset_Manifest entry per subject in the single-table DynamoDB `village` table.

Contract references:
  - DESIGN.md §1 (models/regions), §3 (DynamoDB keys), §4 (Asset_Manifest schema), §5 (API).
  - research/sdxl.md (verified Bedrock Stability request/response schema).
  - requirements.md Requirement 16.

Regions:
  - Image generation runs in us-west-2 (SD models absent in ap-southeast-2).
  - DynamoDB `village` table + S3 assets bucket live in ap-southeast-2.

Environment variables:
  - TABLE_NAME      : DynamoDB table name (default "village").
  - ASSETS_BUCKET   : S3 bucket for generated PNGs (required for live generation).
  - TABLE_REGION    : DynamoDB/S3 region (default "ap-southeast-2").
  - IMAGE_REGION    : Bedrock image region (default "us-west-2").
  - MODEL_ID        : Bedrock model id (default "stability.stable-image-ultra-v1:1").

Handler event shape:
  {"action": "generate_all", "simId": "<id>"}
  {"action": "regenerate",   "simId": "<id>", "subjectId": "<agent_or_loc_id>"}
"""

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone

import boto3

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TABLE_NAME = os.environ.get("TABLE_NAME", "village")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
TABLE_REGION = os.environ.get("TABLE_REGION", "ap-southeast-2")
IMAGE_REGION = os.environ.get("IMAGE_REGION", "us-west-2")
MODEL_ID = os.environ.get("MODEL_ID", "stability.stable-image-ultra-v1:1")

# Prompt / style constraints (Requirement 16.2-16.4)
MAX_PROMPT_CHARS = 1000
MAX_STYLE_CLAUSE_CHARS = 200
MAX_TRAITS = 5

# Aspect ratios (Requirement 16 / task): portraits 1:1, location artwork 3:2.
PORTRAIT_ASPECT = "1:1"
LOCATION_ASPECT = "3:2"

# Retry policy (Requirement 16.8): 1 initial + up to 2 more attempts,
# waiting >= 5s before each retry, and treating a single generation that
# takes > 120s as a failure/timeout.
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 5
GENERATION_TIMEOUT_SECONDS = 120

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Lazy AWS clients (so --selftest never touches AWS)
# --------------------------------------------------------------------------- #

_clients = {}


def _bedrock():
    if "bedrock" not in _clients:
        _clients["bedrock"] = boto3.client("bedrock-runtime", region_name=IMAGE_REGION)
    return _clients["bedrock"]


def _s3():
    if "s3" not in _clients:
        _clients["s3"] = boto3.client("s3", region_name=TABLE_REGION)
    return _clients["s3"]


def _table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource(
            "dynamodb", region_name=TABLE_REGION
        ).Table(TABLE_NAME)
    return _clients["table"]


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #

def _now_iso():
    """Real_Time of storage as an ISO-8601 UTC timestamp (Requirement 16.5)."""
    return datetime.now(timezone.utc).isoformat()


def _seed_for(subject_id):
    """Deterministic non-negative seed in Bedrock's 0..4294967295 range so a
    regeneration of the same subject is reproducible (sdxl.md guidance)."""
    return abs(hash(subject_id)) % 4294967296


# --------------------------------------------------------------------------- #
# Prompt construction (Requirement 16.2 - 16.4)
# --------------------------------------------------------------------------- #

def _apply_style_clause(base_prompt, style_clause):
    """Append the single configured art-style clause character-for-character
    identical across every prompt (Requirement 16.4), keeping the whole prompt
    <= 1000 chars (Requirement 16.2/16.3) by trimming the *base* portion only,
    never the style clause.

    The style clause is enforced to <= 200 chars (Requirement 16.4). We reserve
    room for it (plus a single separating space) and truncate the base prompt if
    needed so the suffix is always present verbatim.
    """
    style_clause = (style_clause or "")[:MAX_STYLE_CLAUSE_CHARS]
    if not style_clause:
        return base_prompt[:MAX_PROMPT_CHARS]

    separator = " "
    reserve = len(separator) + len(style_clause)
    max_base = MAX_PROMPT_CHARS - reserve
    if max_base < 0:
        # Degenerate: clause alone exceeds budget; return clause truncated.
        return style_clause[:MAX_PROMPT_CHARS]
    base = base_prompt[:max_base].rstrip()
    return f"{base}{separator}{style_clause}"


def build_agent_prompt(persona, style_clause):
    """Requirement 16.2 — portrait prompt includes name, age, occupation and up
    to 5 traits; final prompt (incl. style clause) is <= 1000 chars."""
    name = str(persona.get("name", "")).strip()
    age = persona.get("age", "")
    occupation = str(persona.get("occupation", "")).strip()
    traits = [str(t).strip() for t in (persona.get("traits") or []) if str(t).strip()]
    traits = traits[:MAX_TRAITS]

    parts = [f"Character portrait of {name}"]
    if age != "" and age is not None:
        parts.append(f"a {age}-year-old")
    if occupation:
        parts.append(occupation)
    base = ", ".join(parts) + "."
    if traits:
        base += " Personality: " + ", ".join(traits) + "."

    return _apply_style_clause(base, style_clause)


def build_location_prompt(location, style_clause):
    """Requirement 16.3 — artwork prompt includes display name and category;
    final prompt (incl. style clause) is <= 1000 chars."""
    name = str(location.get("name", "")).strip()
    category = str(location.get("category", "")).strip()
    base = f"Illustration of {name}, a {category} location in Melbourne, Australia."
    return _apply_style_clause(base, style_clause)


# --------------------------------------------------------------------------- #
# Bedrock image generation (verified schema — research/sdxl.md)
# --------------------------------------------------------------------------- #

def generate_image(prompt, aspect_ratio=PORTRAIT_ASPECT, seed=0):
    """Generate a PNG via Bedrock Stability in us-west-2. Returns raw PNG bytes.

    Uses the verified request/response schema:
      body   = {"prompt", "aspect_ratio", "output_format":"png", "seed"}
      image  = base64.b64decode(payload["images"][0])
      success = payload["finish_reasons"][0] is None
    Raises RuntimeError if content-filtered.
    """
    body = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "output_format": "png",
        "seed": seed,
    }
    if MODEL_ID.startswith("stability.sd3"):
        body["mode"] = "text-to-image"

    resp = _bedrock().invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        accept="application/json",
        contentType="application/json",
    )
    payload = json.loads(resp["body"].read())
    if payload.get("finish_reasons", [None])[0] is not None:
        raise RuntimeError(f"generation filtered: {payload.get('finish_reasons')}")
    return base64.b64decode(payload["images"][0])


def _generate_with_retry(prompt, aspect_ratio, seed):
    """Requirement 16.8 — retry up to 2 additional times (3 total), waiting
    >= 5s before each retry, and treating a single generation exceeding 120s as
    a timeout failure. Returns PNG bytes or raises the last error."""
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RETRY_WAIT_SECONDS)
        try:
            # Bound a single generation to GENERATION_TIMEOUT_SECONDS.
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(generate_image, prompt, aspect_ratio, seed)
                return fut.result(timeout=GENERATION_TIMEOUT_SECONDS)
        except FutureTimeout as exc:
            last_err = RuntimeError(
                f"generation timed out after {GENERATION_TIMEOUT_SECONDS}s"
            )
            _ = exc
        except Exception as exc:  # noqa: BLE001 - any failure is retryable per R16.8
            last_err = exc
    raise last_err if last_err else RuntimeError("generation failed")


# --------------------------------------------------------------------------- #
# DynamoDB access (single-table `village`, PK=SIM#<simId>)
# --------------------------------------------------------------------------- #

def _pk(sim_id):
    return f"SIM#{sim_id}"


def _load_config(sim_id):
    resp = _table().get_item(Key={"PK": _pk(sim_id), "SK": "CONFIG"})
    return resp.get("Item")


def _art_style_clause(sim_id):
    cfg = _load_config(sim_id) or {}
    return str(cfg.get("artStyleClause", ""))[:MAX_STYLE_CLAUSE_CHARS]


def _query_by_sk_prefix(sim_id, sk_prefix):
    """Enumerate items under one PK whose SK starts with the given prefix."""
    from boto3.dynamodb.conditions import Key

    items = []
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(_pk(sim_id))
        & Key("SK").begins_with(sk_prefix),
    }
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _list_agents(sim_id):
    return _query_by_sk_prefix(sim_id, "AGENT#")


def _list_locations(sim_id):
    return _query_by_sk_prefix(sim_id, "LOC#")


def _get_agent(sim_id, agent_id):
    resp = _table().get_item(Key={"PK": _pk(sim_id), "SK": f"AGENT#{agent_id}"})
    return resp.get("Item")


def _get_location(sim_id, loc_id):
    resp = _table().get_item(Key={"PK": _pk(sim_id), "SK": f"LOC#{loc_id}"})
    return resp.get("Item")


def _get_manifest(sim_id, subject_id):
    resp = _table().get_item(Key={"PK": _pk(sim_id), "SK": f"ASSET#{subject_id}"})
    return resp.get("Item")


def _put_manifest(sim_id, entry):
    """Write one Asset_Manifest entry (DESIGN §4 schema)."""
    item = {
        "PK": _pk(sim_id),
        "SK": f"ASSET#{entry['subjectId']}",
        "schemaVersion": SCHEMA_VERSION,
        "subjectId": entry["subjectId"],
        "subjectType": entry["subjectType"],  # "agent" | "location"
        "prompt": entry["prompt"],            # complete prompt incl. style clause
        "modelId": entry["modelId"],
        "imageKey": entry["imageKey"],
        "storedRealTime": entry["storedRealTime"],
    }
    _table().put_item(Item=item)
    return item


def _put_failure(sim_id, subject_id, subject_type, reason):
    """Requirement 16.11 — record a generation-failure entry (NOT a manifest
    entry). Kept under a distinct SK so it never masquerades as an image."""
    _table().put_item(
        Item={
            "PK": _pk(sim_id),
            "SK": f"ASSETFAIL#{subject_id}",
            "schemaVersion": SCHEMA_VERSION,
            "subjectId": subject_id,
            "subjectType": subject_type,
            "failureReason": str(reason)[:500],
            "recordedRealTime": _now_iso(),
        }
    )


# --------------------------------------------------------------------------- #
# S3 storage
# --------------------------------------------------------------------------- #

def _image_key(sim_id, subject_type, subject_id):
    # subject_type is "agent" | "location"; task keys: sim/<simId>/agent|location/<id>.png
    return f"sim/{sim_id}/{subject_type}/{subject_id}.png"


def _store_png(key, png_bytes):
    if not ASSETS_BUCKET:
        raise RuntimeError("ASSETS_BUCKET is not configured")
    _s3().put_object(
        Bucket=ASSETS_BUCKET,
        Key=key,
        Body=png_bytes,
        ContentType="image/png",
    )
    return key


# --------------------------------------------------------------------------- #
# Subject processing
# --------------------------------------------------------------------------- #

def _agent_subject(agent_item):
    """Normalise an Agent item into (subjectId, persona dict)."""
    subject_id = agent_item.get("subjectId") or agent_item.get("id") \
        or agent_item.get("SK", "").split("#", 1)[-1]
    persona = agent_item.get("persona") or {}
    return subject_id, persona


def _location_subject(loc_item):
    subject_id = loc_item.get("id") or loc_item.get("SK", "").split("#", 1)[-1]
    return subject_id, loc_item


def _process_subject(sim_id, subject_type, subject_id, prompt, aspect_ratio,
                     update_existing=False):
    """Generate → store to S3 → write/update manifest.

    Requirement 16.9 (regeneration): store the replacement image FIRST, then
    update the manifest only after the image is stored. Because a manifest write
    is idempotent (fixed SK), the same "store then write manifest" ordering
    satisfies both first-time creation and regeneration.

    Returns a result dict; on total failure records a generation-failure entry
    and returns status="failed" WITHOUT writing a manifest entry (R16.11).
    """
    seed = _seed_for(subject_id)
    try:
        png = _generate_with_retry(prompt, aspect_ratio, seed)
    except Exception as exc:  # noqa: BLE001
        _put_failure(sim_id, subject_id, subject_type, exc)
        return {"subjectId": subject_id, "subjectType": subject_type,
                "status": "failed", "reason": str(exc)}

    key = _image_key(sim_id, subject_type, subject_id)
    try:
        _store_png(key, png)  # store replacement image first (R16.9)
    except Exception as exc:  # noqa: BLE001
        _put_failure(sim_id, subject_id, subject_type, f"s3 store failed: {exc}")
        return {"subjectId": subject_id, "subjectType": subject_type,
                "status": "failed", "reason": f"s3 store failed: {exc}"}

    entry = {
        "subjectId": subject_id,
        "subjectType": subject_type,
        "prompt": prompt,
        "modelId": MODEL_ID,
        "imageKey": key,
        "storedRealTime": _now_iso(),
    }
    _put_manifest(sim_id, entry)  # update manifest only after image stored (R16.9)
    return {"subjectId": subject_id, "subjectType": subject_type,
            "status": "regenerated" if update_existing else "generated",
            "imageKey": key}


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #

def generate_all(sim_id):
    """Requirement 16.1 — one portrait per Agent + one artwork per Location.

    Continues with remaining subjects when one fails (R16.11). Returns a summary
    dict of per-subject results. Plain callable for local/CLI use.
    """
    style_clause = _art_style_clause(sim_id)
    results = []

    for agent_item in _list_agents(sim_id):
        subject_id, persona = _agent_subject(agent_item)
        if not subject_id:
            continue
        prompt = build_agent_prompt(persona, style_clause)
        results.append(
            _process_subject(sim_id, "agent", subject_id, prompt, PORTRAIT_ASPECT)
        )

    for loc_item in _list_locations(sim_id):
        subject_id, loc = _location_subject(loc_item)
        if not subject_id:
            continue
        prompt = build_location_prompt(loc, style_clause)
        results.append(
            _process_subject(sim_id, "location", subject_id, prompt, LOCATION_ASPECT)
        )

    generated = sum(1 for r in results if r["status"] in ("generated", "regenerated"))
    failed = sum(1 for r in results if r["status"] == "failed")
    return {
        "simId": sim_id,
        "total": len(results),
        "generated": generated,
        "failed": failed,
        "results": results,
    }


def regenerate(sim_id, subject_id):
    """Requirement 16.9 / 16.12 — regenerate a single subject.

    Rejects an unknown subjectId (matches no Agent and no Location) leaving the
    Asset_Manifest unchanged (R16.12). Otherwise stores a replacement image then
    updates the manifest entry only after storage (R16.9).
    """
    if not subject_id:
        return {"ok": False, "error": "subjectId is required for regenerate"}

    style_clause = _art_style_clause(sim_id)

    agent_item = _get_agent(sim_id, subject_id)
    if agent_item is not None:
        _, persona = _agent_subject(agent_item)
        prompt = build_agent_prompt(persona, style_clause)
        result = _process_subject(sim_id, "agent", subject_id, prompt,
                                  PORTRAIT_ASPECT, update_existing=True)
        return {"ok": result["status"] != "failed", "data": result}

    loc_item = _get_location(sim_id, subject_id)
    if loc_item is not None:
        _, loc = _location_subject(loc_item)
        prompt = build_location_prompt(loc, style_clause)
        result = _process_subject(sim_id, "location", subject_id, prompt,
                                  LOCATION_ASPECT, update_existing=True)
        return {"ok": result["status"] != "failed", "data": result}

    # R16.12 — unknown subject: reject, leave manifest unchanged.
    return {"ok": False, "error": f"unknown subjectId: {subject_id}"}


# --------------------------------------------------------------------------- #
# Lambda handler
# --------------------------------------------------------------------------- #

def handler(event, context=None):
    """Lambda entrypoint. event = {action, simId, subjectId?}.

    action ∈ {"generate_all", "regenerate"}.
    """
    event = event or {}
    action = event.get("action", "generate_all")
    sim_id = event.get("simId")

    if not sim_id:
        return _response(400, {"ok": False, "error": "simId is required"})

    try:
        if action == "generate_all":
            data = generate_all(sim_id)
            return _response(200, {"ok": True, "data": data})
        if action == "regenerate":
            result = regenerate(sim_id, event.get("subjectId"))
            status = 200 if result.get("ok") else 400
            return _response(status, result)
        if action == "reseed":
            import reseed as reseed_mod
            data = reseed_mod.reseed(
                sim_id,
                population=event.get("population"),
                use_llm=event.get("useLlm", True),
                generate_assets=event.get("generateAssets", True),
            )
            return _response(200, {"ok": True, "data": data})
        return _response(400, {"ok": False, "error": f"unknown action: {action}"})
    except Exception as exc:  # noqa: BLE001
        return _response(500, {"ok": False, "error": str(exc)})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# --------------------------------------------------------------------------- #
# Self-test (no AWS calls) — python3 index.py --selftest
# --------------------------------------------------------------------------- #

def _selftest():
    style_clause = (
        "in a cohesive hand-painted watercolour storybook style, soft lighting, "
        "muted Melbourne palette, consistent art direction"
    )
    assert len(style_clause) <= MAX_STYLE_CLAUSE_CHARS

    fake_agents = [
        {
            "name": "Aroha Ngata",
            "age": 34,
            "occupation": "Barista",
            "traits": ["warm", "impulsive", "curious", "loyal", "restless", "extra"],
        },
        {
            "name": "X" * 400,  # long name to force base-prompt truncation
            "age": 71,
            "occupation": "Retired tram driver with a very long descriptive title " * 5,
            "traits": ["stoic", "meticulous"],
        },
    ]
    fake_locations = [
        {"name": "Federation Square", "category": "leisure"},
        {"name": "Queen Victoria Market", "category": "retail"},
    ]

    prompts = []
    for a in fake_agents:
        p = build_agent_prompt(a, style_clause)
        prompts.append(p)
        # <= 1000 char limit (R16.2)
        assert len(p) <= MAX_PROMPT_CHARS, f"agent prompt too long: {len(p)}"
        # includes name (when it fits), age, occupation
        assert str(a["age"]) in p, "age missing from agent prompt"
        # at most 5 traits included (R16.2)
        included = [t for t in a["traits"] if t in p]
        assert len(included) <= MAX_TRAITS, "more than 5 traits in prompt"

    for loc in fake_locations:
        p = build_location_prompt(loc, style_clause)
        prompts.append(p)
        assert len(p) <= MAX_PROMPT_CHARS, f"location prompt too long: {len(p)}"
        assert loc["name"] in p, "location name missing"
        assert loc["category"] in p, "location category missing"

    # Every prompt ends with the identical style clause, character-for-character (R16.4)
    for p in prompts:
        assert p.endswith(style_clause), "style clause suffix not identical / missing"

    # Empty-clause path still respects the 1000-char cap.
    p_noclause = build_agent_prompt(fake_agents[0], "")
    assert len(p_noclause) <= MAX_PROMPT_CHARS

    # Over-long clause is truncated to 200 chars and still appended identically.
    long_clause = "z" * 500
    trimmed = long_clause[:MAX_STYLE_CLAUSE_CHARS]
    p_long = build_location_prompt(fake_locations[0], long_clause)
    assert p_long.endswith(trimmed), "over-long clause not truncated to 200 & appended"
    assert len(p_long) <= MAX_PROMPT_CHARS

    print("SELFTEST PASSED: %d prompts built; all <= %d chars; identical style suffix."
          % (len(prompts), MAX_PROMPT_CHARS))
    return True


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    if "--generate-all" in sys.argv:
        idx = sys.argv.index("--generate-all")
        sim = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        print(json.dumps(generate_all(sim), indent=2))
        sys.exit(0)
    print("usage: python3 index.py [--selftest | --generate-all <simId>]")
