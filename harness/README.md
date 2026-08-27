# Agent Harness — Bedrock AgentCore Runtime container

The **Agent_Harness** is the stateless per-call reasoning component of the
Melbourne Agent Village (DESIGN.md §2). It runs as a **Bedrock AgentCore Runtime**
container (ARM64) and, on each invocation, calls the right Bedrock model and
reads/writes AgentCore Memory, returning a structured response the simulation
engine validates per Requirement 6.5.

## Container contract (AgentCore Runtime)

Per `.orchestrator/research/agentcore.md` §1a the container:

- is **linux/arm64** and listens on **port 8080**;
- implements **`GET /ping`** → `{"status":"healthy"}` (HTTP 200 health check);
- implements **`POST /invocations`** → receives the JSON request (DESIGN §6),
  returns the JSON response.

The runtime session id is passed in header
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`; the engine sets it to
`<agentId>+<simDay>`. It is echoed back on the response as `_sessionId`.

```
uvicorn harness:app --host 0.0.0.0 --port 8080
```

## Models (DESIGN §1, region ap-southeast-2)

| Op | Model | Purpose label (Req 18.3) |
|----|-------|--------------------------|
| decision | `au.anthropic.claude-opus-5` (reasoning) | `decision_cycle` |
| plan | `au.anthropic.claude-opus-5` | `decision_cycle` |
| reflect | `au.anthropic.claude-opus-5` | `reflection` |
| utterance | `au.anthropic.claude-haiku-4-5-20251001-v1:0` (fast) | `conversation` |

All calls use the Bedrock **Messages API** with
`anthropic_version="bedrock-2023-05-31"` and an enforced `max_tokens`.
Bedrock throttling is retried up to **5 times** with exponential backoff
(1s → doubling → 30s max) plus 0–50% jitter (Req 18.7).

## Request (engine → harness, DESIGN §6)

```json
{"op":"decision|plan|reflect|utterance",
 "simId":"...","agentId":"...","simTime":"2026-03-02T08:15:00+11:00",
 "persona":{...}, "state":{...},
 "currentLocation":{...}, "coLocated":[{"id","name","actionType"}],
 "reachable":[{"id","name","category","hours","remainingCapacity","travelMin"}],
 "shortTermMemory":[...<=50], "longTermMemory":[...<=20],
 "perceptionFlags":{"criticalNeeds":[...],"financialPressure":"high|normal",
    "pendingInvestigation":{...}?, "rejectedPurchase":true?, "employmentOffers":[...]?},
 "priceTable":{"<actionType>":<price>}, "failedValidation":null|"...",
 "conversation":{"participants":[...],"utterancesSoFar":[...]}   // op=utterance only
}
```

## Response by op (DESIGN §6)

- **decision** →
  ```json
  {"action":{"type":"sleep|eat|work|travel|socialise|shop|leisure|commit_crime|idle",
             "targetType":"location|agent","targetId":"...",
             "expectedDurationMin":<1..600>,"crimeType":"theft|burglary|vandalism|fraud"?}}
  ```
- **plan** → `{"plan":[{"type","targetType","targetId"} × 3..12]}`
  (empty `plan` + `"planFailed":true` when the model yields fewer than 3 valid
  items, so the engine can apply Req 6.11).
- **reflect** → `{"reflections":[{"text":"<=500 chars","sourceMemoryIds":[1..20 ints]} × 1..5]}`
  (empty `reflections` + `"reflectFailed":true` on failure → Req 7.9).
- **utterance** → `{"utterance":"<=500 chars"}`

### Common fields on every response

- `"tokenUsage":{"inputTokens","outputTokens","modelId","purpose"}` — counted
  from the Bedrock response `usage` block so the engine's `Budget_Accountant`
  can do budget accounting (DESIGN §6 / Req 18.3). `purpose` matches
  `budget.VALID_PURPOSES`.
- `"memoryDegraded": true` — present only when an AgentCore Memory op failed
  after retries (Req 7.7); the engine-supplied memory in the payload is used
  instead and the decision still succeeds.
- `"_sessionId"` — the echoed runtime session id header.

## Robust output parsing (Req 6)

The model is prompted JSON-mode style ("reply with ONLY a JSON object").
`parse_json_object` tolerates markdown fences and surrounding prose. If the
first reply is unparseable the harness **re-asks once** with a stricter
instruction; if still unparseable it falls back to a **safe idle action**
(`idle`, 10 min, current location) for decisions, or an empty
`plan`/`reflections` with the failure flag for plan/reflect. The harness never
crashes the runtime — the `/invocations` handler degrades to a safe idle action
on any unexpected error.

## Memory (Req 7) — AgentCore Memory data plane

Env var **`MEMORY_ID`** points at the AgentCore Memory resource. When set, the
harness treats it as the durable store:

- **READ** on every op: `list_events` for the current day
  (`sessionId="day-<simDate>"`, ≤50) and `retrieve_memory_records`
  (semantic, `namespace="village/{agentId}/semantic"`, `topK<=10`).
- **WRITE** with `create_event` (`actorId=agentId`,
  `sessionId="day-<simDate>"`, conversational payload): decisions and plans as
  short-term events; reflections fed as long-term-bound events.

Every memory op is **guarded**: on error/timeout it retries ≤2 times, then
continues using the payload-supplied memory and sets `memoryDegraded:true`. A
Memory failure never fails a decision (Req 7.7/7.8). When `MEMORY_ID` is unset
the harness uses only the payload-supplied memory.

## Environment variables

| Var | Meaning | Default |
|-----|---------|---------|
| `AWS_REGION` | Bedrock + AgentCore region | `ap-southeast-2` |
| `MEMORY_ID` | AgentCore Memory resource id | *(unset → payload memory only)* |

## Build & deploy (ARM64 → ECR → AgentCore Runtime)

```bash
docker build --platform linux/arm64 -t agent-harness .
# push to 490004615937.dkr.ecr.ap-southeast-2.amazonaws.com/agent-harness:latest
# create-agent-runtime with containerConfiguration.containerUri (see agentcore.md §1e)
```

## Verify (no deploy)

```bash
python3 harness.py --selftest
```

The self-test monkeypatches a stubbed Bedrock client returning canned Claude
Messages bodies and asserts: prompt builders include persona/needs/critical
flags/reachable locations/failedValidation; JSON parsing handles fenced /
prose-wrapped / plain / unparseable input; each op returns a schema-valid
response with populated `tokenUsage`; unparseable output falls back to a safe
idle action; the throttling retry backoff schedule and retry loop work; and a
failing Memory client sets `memoryDegraded` without failing the decision.
