# Simulation_API Lambda — Melbourne Agent Village

Single Python Lambda behind an **API Gateway HTTP API** (payload format v2.0)
that serves the Visualisation_Client SPA and issues control commands to the
Simulation Engine. It is the authenticated read/control surface defined in
`DESIGN.md §5`. Region: **ap-southeast-2**.

The handler has **no dependency on the engine package** — it is fully
self-contained so it can be zipped and deployed on its own. The small
location open/closed/at_capacity status check is reimplemented here (mirrors
`engine/village/timeutil.py`) to avoid bundling the engine.

## Endpoints (base path `/v1`, envelope `{ok, data|error}`)

| Method | Path | Purpose | Req |
|---|---|---|---|
| POST | `/v1/sim/{simId}/control` | Write a `CONTROL` item (`command`, `requestedAt`, `nonce`) the engine consumes; return current `STATUS`. Rejects invalid commands. | 2 |
| GET | `/v1/sim/{simId}/summary` | Legal/employment counts + crime & conversation counts for the current sim day. | 14.9 |
| GET | `/v1/sim/{simId}/state` | Compact snapshot for the SPA poll: `simTime,status,accel,agents[],conversations[]`. | 13, 15 |
| GET | `/v1/sim/{simId}/agents/{agentId}` | Full persona + state + last 10 events (via GSI2). | 15.6 |
| GET | `/v1/sim/{simId}/locations` | All locations with computed `open/closed/at_capacity` status + present agents. | 15.7, 3 |
| GET | `/v1/sim/{simId}/locations/{locId}` | Single-location detail. | 15.7 |
| GET | `/v1/sim/{simId}/events` | Filtered event query (`category`,`agentId`,`fromSimTime`,`toSimTime`,`cursor`); ≤500 ascending + `more` flag; empty (not error) when none. | 14.2/14.3 |
| GET | `/v1/sim/{simId}/events/decision-trail?actionEventSeq=` | Decision trail for one action event; 404 + `data:null` when absent. | 14.4/14.5 |
| GET | `/v1/sim/{simId}/cost` | Cost report from `model` events broken down by `modelId` and `purpose`, USD 2dp; zeros (not error) when empty. | 18.9 |
| GET | `/v1/sim/{simId}/assets/{subjectId}` | 302 redirect to a presigned S3 URL for the manifest `imageKey`; 404-style `{ok:false}` when no manifest/object. | 16.6/16.10 |
| POST | `/v1/sim/{simId}/config` | Validate + store `Config` incl. budget; set `seedPending` flag for the seed process. | 4, 18.1/18.2 |
| POST | `/v1/sim/{simId}/assets/generate` | Async-invoke the Asset_Generator (`ASSET_FN_NAME`) or write a trigger item; returns 202. | 16 |

`OPTIONS` on any path returns `204` with CORS headers (preflight, no auth).

## Authentication (Req 17.4 / 17.5)

The HTTP API is fronted by a **Cognito JWT authorizer**, so API Gateway places
validated claims at `event.requestContext.authorizer.jwt.claims`. The handler
confirms a **non-empty** claims object is present **before any DynamoDB read or
write** and returns `401 {ok:false,error:...}` otherwise. Because API Gateway
only forwards the request after the authorizer validates the token, the presence
of claims is proof of a valid caller identity.

- `ALLOW_ANON=1` — **local-testing escape hatch only**, default **off**. When
  set, the auth gate is bypassed. Never set this in a deployed stack.

## DynamoDB single-table (`village`) access (DESIGN §3)

- `PK = SIM#<simId>`; item types by `SK` prefix (`STATUS`, `CONFIG`, `CONTROL`,
  `AGENT#`, `LOC#`, `EVENT#<seq20>`, `ASSET#`).
- Event queries use **GSI1** (`GSI1PK = SIM#<id>#CAT#<category>`) for category
  filters, **GSI2** (`GSI2PK = SIM#<id>#AGENT#<agentId>`) for agent filters, and
  a base-table `begins_with(EVENT#)` query otherwise. Time-range and combined
  filters are applied in Python; results are sorted ascending by `(simTime, seq)`.
- DynamoDB `Decimal` numbers are JSON-encoded as ints (when integral) or floats.
- Config writes convert numeric leaves to `Decimal` (DynamoDB requirement).

## Environment variables (set by the CDK stack)

| Var | Meaning | Default |
|---|---|---|
| `TABLE_NAME` | DynamoDB table | `village` |
| `ASSETS_BUCKET` | S3 bucket for generated PNGs | *(required for asset serving)* |
| `ASSET_FN_NAME` | Asset_Generator Lambda name (async invoke) | *(empty → trigger item)* |
| `AWS_REGION` | provided by the Lambda runtime | `ap-southeast-2` |
| `ALLOW_ANON` | `1` to bypass auth (local only) | off |

## Handler contract

- Entry point: `index.handler(event, context)`.
- CORS: permissive (`Access-Control-Allow-Origin: *`) since the SPA is on a
  different origin.
- Errors are returned as `{ok:false,error:...}` with an appropriate status
  (400 validation, 401 auth, 404 not found, 500 internal, 502 downstream).

## Cost report math (Req 18.9)

For each `model`-category event in the requested `simTime` range, spend is
computed as `inputTokens/1000 * per1kInput + outputTokens/1000 * per1kOutput`
using prices from the stored `CONFIG.budget.prices`. Per-bucket raw `Decimal`
totals are summed and rounded **once** to 2dp (`ROUND_HALF_UP`). Missing prices
contribute `0`. An empty range returns all-zero values, never an error.

## Testing (no deployment)

`test_api.py` builds fake API Gateway v2 proxy events and invokes `handler()`
with a monkeypatched in-memory DynamoDB/S3/Lambda (no AWS calls). It asserts
routing, auth rejection (missing claims → 401 with **zero** table access),
envelope shape, control write + invalid-command rejection, cost aggregation
math, summary window, event filtering/paging, decision trail present/absent,
agent/location detail, and asset 302/404.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install boto3 pytest
python -m pytest test_api.py -v
```

Result: **35 passed**.

## Notes / deviations

- **Control legality**: the API validates only that the command is one of
  `start|pause|resume|stop` (rejecting anything else with 400). Whether a
  command is legal for the current status (Req 2.7) is enforced authoritatively
  by the engine when it consumes the `CONTROL` item, per DESIGN §5 ("API just
  writes it and returns current STATUS"). The API returns the current `STATUS`
  in every control response so the SPA can reflect state.
- **Population generation** is heavy and runs out-of-band: `POST /config`
  validates + stores the config and sets a `seedPending` flag that the seed
  process consumes to write agents (per the task's allowance).
- **Conversations** in `/state` and the `/summary` conversation count are
  derived from the engine's event/state conventions: `/state` reads a
  `state.conversation` grouping if present; `/summary` counts
  `conversation`-category events flagged `conversation-ended` (falling back to
  all conversation entries if the engine emits one entry per conversation).
- **Asset serving** issues a **302 redirect** to a short-lived presigned S3 URL
  (rather than streaming bytes) — lower latency, keeps large payloads out of the
  Lambda response, and stays well within the 2s budget. It `head_object`s first
  so it never redirects to a missing object (404 instead, Req 16.10).
