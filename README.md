# Melbourne Agent Village

A living, simulated Melbourne rendered as a cozy storybook map. Autonomous
agents move around the city, work, eat, socialise, shop, and occasionally get
into trouble — all driven by an LLM-backed simulation engine. Operators watch
the world unfold in real time through a web client and can start/pause/resume/stop
the simulation.

The system runs in a single AWS account and region (**ap-southeast-2 / Sydney**),
except Stable Diffusion image generation, which targets us-west-2.

---

## Architecture

```
┌─────────────┐     HTTPS      ┌──────────────────┐   JWT    ┌────────────────┐
│  Web SPA    │ ─────────────► │  API Gateway     │ ───────► │  API Lambda    │
│ (CloudFront │  /v1/sim/...   │  (HTTP API v2)   │  authz   │  (Python 3.12) │
│  + S3)      │ ◄───────────── │  Cognito JWT     │          └──────┬─────────┘
└─────────────┘   JSON/302     └──────────────────┘                 │
                                                          ┌──────────┴─────────┐
                                                          │  DynamoDB (single  │
                                                          │  table + GSI1/2)   │
                                                          └──────────┬─────────┘
┌──────────────────┐  reads/writes world state                      │
│ Simulation Engine│ ◄──────────────────────────────────────────────┘
│ (ECS Fargate)    │  invokes agent reasoning via Bedrock AgentCore
└──────────────────┘
                     ┌────────────────────┐   presigned    ┌──────────────────┐
   Asset Generator ─►│ Assets S3 bucket   │ ─────────────► │ Web SPA <img>    │
   (Lambda, Bedrock) │ (private, CORS GET)│   302 redirect └──────────────────┘
   Stable Diffusion  └────────────────────┘
```

### Components

| Path | What it is |
|------|------------|
| `web/` | React + Vite + TypeScript SPA. Leaflet map bounded to Melbourne, live polling of world state, agent/location detail panels, operator controls. Hosted on S3 behind CloudFront. |
| `api/` | Python Lambda behind an API Gateway HTTP API (payload v2.0). Serves `/v1/sim/{simId}/...` (state, agents, locations, control, events, assets). Cognito JWT authorizer; every response is `{ ok, data }` or `{ ok:false, error }`. |
| `engine/` | The simulation engine (ECS Fargate, ARM64). Ticks the world clock and updates agent needs, movement, economy, social, crime, and law in DynamoDB. Pure-Python core with a full unit-test suite under `engine/tests/`. |
| `harness/` | Agent-reasoning harness container image (ARM64), run out-of-band as a Bedrock AgentCore Runtime and invoked by the engine. |
| `assets/` | Asset Generator Lambda. Generates agent/location portraits via Bedrock (Stable Diffusion, us-west-2) and stores PNGs in the private assets S3 bucket. |
| `seed/` | Persona/location/job seed data and scripts (`seed.py`, `generate_personas.py`) plus `config.json` (sim parameters, budget, decay rates, population). |
| `infra/` | AWS CDK app (TypeScript). Provisions everything: DynamoDB, S3 buckets, Lambdas, Cognito, API Gateway, ECS/Fargate, CloudFront. |

---

## API surface

Base URL: the API Gateway endpoint. All paths are prefixed `/v1/sim/{simId}`
(default `simId` = `melb`). Auth: `Authorization: Bearer <Cognito IdToken>`.

| Method & path | Purpose |
|---------------|---------|
| `GET /v1/sim/{simId}/state` | Compact world snapshot (agents, positions, actions, conversations) for the poll loop. |
| `GET /v1/sim/{simId}/agents/{agentId}` | Full agent detail: persona, needs, cash, employment/legal status, recent events. |
| `GET /v1/sim/{simId}/locations` | All locations with status + present agents. |
| `GET /v1/sim/{simId}/locations/{locId}` | Location detail. |
| `GET /v1/sim/{simId}/assets/{subjectId}` | 302 redirect to a presigned S3 URL for an agent/location portrait. |
| `POST /v1/sim/{simId}/control` | Issue `start` / `pause` / `resume` / `stop`. |
| `GET /v1/sim/{simId}/events` | Filtered event-log query. |

`OPTIONS` (CORS preflight) is answered by the API's built-in CORS handling and
is intentionally **not** behind the JWT authorizer.

---

## Prerequisites

- Node.js 18+ and npm
- Python 3.12
- AWS CLI configured for account `490004615937` / region `ap-southeast-2`
- AWS CDK v2 (`npx cdk`)
- Docker (for building the engine/harness container images)

---

## Local development (web)

The SPA can run entirely offline against a built-in mock backend — no AWS, no
auth:

```bash
cd web
npm install
# Mock mode: set VITE_MOCK=1 in .env.local (see .env.example)
npm run dev
```

To run against the live backend, copy `.env.example` to `.env.local`, set
`VITE_MOCK=0`, and provide `VITE_API_BASE_URL`, `VITE_COGNITO_REGION`, and
`VITE_COGNITO_CLIENT_ID`.

Build for production:

```bash
npm run build     # tsc -b && vite build  → web/dist
```

> **Security note:** `web/.env.production` is git-ignored because it contains
> real operator credentials that get baked into the public JS bundle. For
> anything beyond a demo, replace embedded operator creds with a proper
> interactive login flow and rotate any exposed credentials.

---

## Deploy (infra)

```bash
cd infra
npm install
# Optional context overrides: -c env=dev|test|prod  -c simulationId=melb
npx cdk deploy VillageStack-dev --require-approval never
```

Key CloudFormation outputs: `ApiEndpoint`, `UserPoolId`, `UserPoolClientId`,
`SpaUrl`, `SpaBucketName`, `AssetsBucketName`, `TableName`,
`EngineClusterName`, `HarnessImageUri`.

Deploying the SPA: build `web/dist`, then re-deploy (the stack uploads
`web/dist` to the SPA bucket and invalidates CloudFront) or sync `web/dist` to
the `SpaBucketName` bucket directly.

---

## Tests

```bash
# API Lambda
cd api && python -m pytest -q            # 35 tests

# Simulation engine
cd engine && python -m pytest -q
```

---

## Simulation configuration

`seed/config.json` controls the world: `simId`, timezone
(`Australia/Melbourne`), acceleration factor, need decay/recovery rates,
initial needs, population (25 agents), the detention facility, the Bedrock
budget guardrails (`maxInvocationsPerSimHour`, `maxSpendUSD`), and the
storybook art-style prompt used for asset generation.

---

## Notes

- CORS is currently `*` on the API and assets bucket — fine for a dev demo;
  scope it to the CloudFront domain for production.
- Bedrock AgentCore Runtime/Memory are created out-of-band from the harness
  image and passed to the engine via CloudFormation parameters.
