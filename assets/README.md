# Asset_Generator Lambda

Generates game assets for the **Melbourne Agent Village** — one portrait per Agent
and one artwork per Location — using Stable Diffusion on Amazon Bedrock, storing PNGs
to S3 and recording an `Asset_Manifest` entry per subject in DynamoDB.

Implements **Requirement 16** of `requirements.md` and conforms to `DESIGN.md`
§1 (models/regions), §3 (DynamoDB keys), §4 (Asset_Manifest schema), and §5 (API).

## Model & regions

- Image generation: **Bedrock `stability.stable-image-ultra-v1:1`** (SD3.5 architecture)
  in **`us-west-2`** (the SD models are absent in `ap-southeast-2`).
  - Request body: `{"prompt", "aspect_ratio", "output_format": "png", "seed"}`.
  - Image bytes: `base64.b64decode(response["images"][0])`.
  - Success check: `response["finish_reasons"][0] is None` (non-null = content-filtered).
  - Verified schema in `.orchestrator/research/sdxl.md`.
- DynamoDB `village` table **and** the S3 assets bucket live in **`ap-southeast-2`**.
- Aspect ratios: portraits `1:1`, location artwork `3:2`.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `TABLE_NAME` | `village` | DynamoDB single table (PK=`SIM#<simId>`) |
| `ASSETS_BUCKET` | *(none)* | S3 bucket for PNGs (required for live generation) |
| `TABLE_REGION` | `ap-southeast-2` | DynamoDB + S3 region |
| `IMAGE_REGION` | `us-west-2` | Bedrock image region |
| `MODEL_ID` | `stability.stable-image-ultra-v1:1` | Bedrock model id |

The CDK stack (built separately) provisions the bucket, table, and IAM roles. This
Lambda creates no infrastructure.

## Handler

`handler(event, context)` — entrypoint. `event`:

```json
{"action": "generate_all", "simId": "melb"}
{"action": "regenerate",   "simId": "melb", "subjectId": "agent_01"}
```

Returns an API-Gateway-style `{statusCode, headers, body}` with `{ok, data|error}`.

`generate_all(simId)` is also exposed as a plain callable for local/CLI use.

## Behaviour (Requirement 16)

- **16.1** — one portrait per Agent (`SK AGENT#…`) + one artwork per Location (`SK LOC#…`).
- **16.2/16.3** — portrait prompt = persona name, age, occupation, ≤5 traits; location
  prompt = display name + category. Every prompt ≤ 1000 chars.
- **16.4** — the single configured `artStyleClause` (from the `CONFIG` item, ≤200 chars)
  is appended character-for-character identical to **every** prompt. The base prompt is
  truncated (never the clause) to keep within 1000 chars.
- **16.5** — stores the PNG to S3 at `sim/<simId>/agent|location/<id>.png` and writes one
  manifest item (`SK ASSET#<subjectId>`) with `subjectId`, `subjectType` (`agent|location`),
  the complete `prompt`, `modelId`, `imageKey`, `storedRealTime`.
- **16.8** — a failing or >120s generation is retried up to 2 more times (3 total),
  waiting ≥5s before each retry.
- **16.11** — on total failure, records a **generation-failure** item (`SK ASSETFAIL#<id>`,
  NOT a manifest entry) and continues with remaining subjects.
- **16.9** — regeneration stores the replacement image first, then updates the manifest
  entry only after the image is stored (idempotent fixed-SK write).
- **16.12** — regeneration for an unknown `subjectId` (no matching Agent or Location) is
  rejected and the manifest is left unchanged.

## Local usage

```bash
# Prompt-building self-test — no AWS calls:
python3 index.py --selftest

# Generate all assets for a simulation (requires AWS creds + env vars):
python3 index.py --generate-all melb
```

## Deploy dependencies

`requirements.txt` pins `boto3`. The Lambda runtime already ships boto3, but it is
listed for local runs and reproducible packaging.
