# Melbourne Agent Village — Simulation Engine

Deterministic, pure-Python simulation domain library for the Melbourne Agent
Village. It owns the authoritative in-memory world state and persists to
DynamoDB. Designed to run as a single long-running container on **ECS Fargate**
(linux/amd64) per `.orchestrator/DESIGN.md` §2.

Python 3.12+, standard library + `boto3` only. All non-deterministic inputs
(real clock, RNG, Bedrock/AgentCore calls, DynamoDB) are injected so the domain
logic is fully unit-testable.

## Layout

```
engine/
  village/            importable domain library
    __init__.py       schema version + map-bounds + timezone constants
    models.py         dataclasses + enums for every entity (DESIGN §4 schemas)
    clock.py          Simulation_Clock — accelerated clock (Req 1)
    needs.py          needs decay/recovery (Req 5)
    movement.py       Movement_Engine — routing, modes, interpolation (Req 8)
    timeutil.py       Melbourne-tz location status + next-opening (Req 3)
    economy.py        Economy_Engine — jobs/wages/purchases/costs (Req 9)
    social.py         Social_Engine — conversations & relationships (Req 10)
    crime.py          Crime_Engine — validation, likelihood, transfers (Req 11)
    law.py            Law_Enforcement_Engine — detection & consequences (Req 12)
    state.py          Serializer/Parser + DynamoStore (Req 13)
    eventlog.py       Event_Log — append-only observability (Req 14)
    budget.py         invocation cap + spend accounting + retry (Req 18)
    controller.py     Simulation_Controller — lifecycle (Req 2)
    ticker.py         tick loop + AgentRuntimeClient Protocol (DESIGN §8)
  tests/              pytest unit tests
  main.py             Fargate entrypoint (env config + CONTROL polling)
  Dockerfile          python:3.12-slim, linux/amd64
  requirements.txt    boto3, pytest
```

## Requirement → module map

| Req | Component | Module |
|-----|-----------|--------|
| 1  | Accelerated clock | `clock.py` |
| 2  | Lifecycle control | `controller.py` |
| 3  | Location status | `timeutil.py`, `models.py` |
| 4  | Persona/config validation | `controller.py`, `models.py` |
| 5  | Needs | `needs.py` |
| 6  | Decision cycle wiring | `ticker.py` (+ `AgentRuntimeClient`) |
| 8  | Movement | `movement.py` |
| 9  | Economy | `economy.py` |
| 10 | Social | `social.py` |
| 11 | Crime | `crime.py` |
| 12 | Law enforcement | `law.py` |
| 13 | Persistence | `state.py` |
| 14 | Event log | `eventlog.py` |
| 18 | Budget & throughput | `budget.py` |

Requirements 7 (Memory), 15 (Visualisation), 16 (Assets), 17 (IAM/platform) live
in other components (`harness/`, `web/`, `assets/`, `infra/`); the engine
exposes the interfaces they plug into (`AgentRuntimeClient`, `DynamoStore`,
event/model records).

## Running tests

```bash
cd app/engine
python3 -m venv .venv
. .venv/bin/activate
pip install boto3 pytest
python3 -m pytest -q
```

## Injected dependencies (determinism)

- `Simulation_Clock(real_clock=...)` — monotonic-seconds callable.
- `Crime_Engine(rng=...)` — `random.Random` for success rolls.
- `Ticker(runtime=..., persist=...)` — `AgentRuntimeClient` Protocol + persist hook.
- `DynamoStore(table=..., sleep=...)` — injectable boto3 Table (or fake) + sleep.
- `Event_Log(sink=..., real_clock=...)` — pluggable sink + wall clock.

## Container / entrypoint

`main.py` reads `TABLE_NAME`, `SIM_ID`, `AGENT_RUNTIME_ARN`, `AWS_REGION`,
`MEMORY_ID`, loads the world from DynamoDB, constructs the `Ticker`, and loops:
poll `CONTROL` item → apply control command → advance one iteration → write
`STATUS`. Build with `docker build --platform linux/amd64 -t village-engine .`.
