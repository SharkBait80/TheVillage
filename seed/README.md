# Melbourne Agent Village — Seed Data

Seed data and loader for the `melb` simulation. All coordinates are real
Melbourne CBD / inner-suburb WGS84 locations within the map bounds
lat `[-38.00, -37.70]`, lon `[144.85, 145.10]` (Requirement 3).

## Files

| File | Purpose |
|---|---|
| `locations.json` | 49 curated real Melbourne locations across all 7 categories (`residence`, `workplace`, `food`, `retail`, `leisure`, `transit`, `civic`). One civic location (`loc_remand`, Melbourne Assessment Prison) is marked `isDetentionFacility: true`. Food/retail carry a `price`. |
| `jobs.json` | 24 Jobs attached to workplace/food/retail/civic locations, with wages 15–200 AUD/hr, shift start/duration, and occupations matching the persona occupation pool. |
| `config.json` | The `Config` item (DESIGN.md §4): accel 4, `startSimTime` a Monday 06:00 AEDT (`2026-03-02T06:00:00+11:00`), detention facility, art-style clause, decay/recovery/needs defaults, budget block and model prices. |
| `generate_personas.py` | `generate_personas(count, locations, seed=42) -> list[dict]` producing merged Persona + Agent_State agent dicts. stdlib only, deterministic. |
| `seed.py` | Loader: validates all data against Req 3 & 4, generates personas, assigns jobs, and writes items to DynamoDB using the DESIGN.md key schema. |

## Schema conformance

Items are written under `PK = SIM#<simId>` with `SK`:
`CONFIG`, `STATUS`, `LOC#<id>`, `AGENT#<id>`, `JOB#<id>`, `REL#<from>#<to>`
— matching DESIGN.md §3. All payloads carry `schemaVersion: 1`. Floats are
converted to `Decimal` for DynamoDB.

## Usage

Validate everything without touching AWS (recommended first step):

```bash
python3 seed.py --dry-run
```

Load into DynamoDB:

```bash
python3 seed.py --table village --sim-id melb
```

Optional population override (5–100, default 25):

```bash
python3 seed.py --table village --sim-id melb --population 40
```

## Validation

`seed.py` enforces, before any write:

- **Req 3 (locations):** 30–500 locations; ≥2 per category; unique ids; name
  1–80 chars; lat/lon within bounds and written to ≥6 decimal places; capacity
  1–5000; 7 days of HH:MM hours; food/retail price 0.01–999.99; exactly one
  detention facility (civic).
- **Req 4 (personas):** 5–100 agents; unique case-insensitive trimmed names;
  age 18–85; occupation 1–80 chars; 3–6 traits (1–40 chars each); background
  1–1000 chars; `homeLocationId` is a residence; 0–10 relationships;
  provenance `generated`.
- **Req 4.7 / 5.8 / 9.1 (initial state):** needs 60–90; `legalStatus` clear;
  `employmentStatus` employed iff occupation matches a Job; cash 50–500 AUD;
  position = home; wakeTime 06:00–09:00; dailyLivingCost 20–80 AUD.

Determinism: identical output for a given `seed`.

## Notes

- Model prices in `config.json` for `au.anthropic.claude-opus-5`
  (~0.015/0.075 per 1k) and `au.anthropic.claude-haiku-4-5-20251001-v1:0`
  (~0.001/0.005 per 1k) are **estimates** (flagged with a `note` field) — the
  exact Bedrock ap-southeast-2 prices should be confirmed and updated.
- With the default population of 25, ~76% of agents (19) are employed and all
  are assigned matching jobs.
