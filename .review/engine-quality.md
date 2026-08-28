# Simulation Engine — Code-Quality & Correctness Review

**Scope:** `engine/main.py`, `engine/village/*.py`, `harness/harness.py`
**Focus:** race conditions, concurrency/memory at scale, retry/backoff, tick-loop resilience, budget guardrails, blocking I/O, time/timezone bugs, single-agent-failure blast radius.
**Method:** full read of the engine + harness sources; external validation of boto3 thread-safety / connection-pool defaults, botocore adaptive retry, and Python 3.12 concurrency semantics (2024–2026 references).
**Runtime note:** the deployed target is Python 3.12 (Dockerfile/README), but the local `__pycache__` shows the tests were last run under CPython 3.9 (`*.cpython-39.pyc`). Several findings below hinge on this gap (see F2, F16).

---

## Severity summary

| # | Severity | Area | File / line | One-line |
|---|----------|------|-------------|----------|
| F1 | High | Concurrency / memory | `ticker.py:2/560` + others | Abandoned harness threads accumulate under load → thread & memory leak, hidden write races on shared world state |
| F2 | High | Concurrency (Py version) | `ticker.py:335` | `ThreadPoolExecutor.shutdown(cancel_futures=…)` requires Py3.8+; `as_completed(timeout=…)` leaves in-flight futures mutating agents *after* the tick continues |
| F3 | High | Budget guardrail | `budget.py:112`, `ticker.py:236/292/430` | Budget cap is checked **before** the call but counted **after**; concurrent fan-out + async token accounting lets spend/invocations overshoot the cap |
| F4 | High | Tick-loop resilience | `main.py` main loop | Reseed/`load_world` and per-tick `refresh_injected_events` do unbounded synchronous DynamoDB scans on the hot path → clock stalls at scale |
| F5 | High | Memory growth at 500 agents | `ticker.py:_stm`, `WorldState.processedEventIds`, `AgentState.creditedConversations` | Several per-agent buffers grow without global bound; `creditedConversations` is persisted every tick and grows forever |
| F6 | High | Persistence write amplification | `ticker.py:_process_tick` step 6 | Every agent is `put_item`-ed every tick regardless of change → ~500 writes/tick, no batching, no change-detection |
| F7 | Med | Retry/backoff | `state.py:DynamoStore.put` | `put` retries on **all** exceptions (incl. validation/`ConditionalCheckFailed`) and blocks the whole tick loop for up to 7s; `get`/`query` have **no** retry |
| F8 | Med | Time / timezone | `law.py:check_release/autoclear`, `economy.shift_overlap_minutes` | Naive `datetime.fromisoformat` on persisted strings + `localize(sim_time)` mixes aware/naive; DST/offset edge cases and shift windows crossing midnight are wrong |
| F9 | Med | Clock correctness | `clock.py:advance` / `_accrue` | `_accrue` advances `_sim_time` but never updates `_last_boundary`; interacts with `advance` to risk a burst of catch-up ticks; unbounded `ticks` list on a long stall |
| F10 | Med | Single-agent failure | `ticker.py:_process_tick` steps 1,4,5,6 | Needs/day-rollover/law/persist loops are **not** individually guarded; one malformed agent throws and aborts the entire tick |
| F11 | Med | Blocking I/O in loop | `main.py`, `budget.invoke_with_retry`, `state.RETRY_BACKOFF` | Synchronous `time.sleep` backoffs and network calls run on the single tick thread; a throttling storm freezes the world clock |
| F12 | Med | Budget hour reset | `budget.py:on_tick` | Invocation cap resets per **sim** hour, not wall-clock; at accel=4 a real minute can silently reset the cap, defeating rate-limiting |
| F13 | Low | Latent NameError | `ticker.py:614` | `_conversation_turn_band -> Tuple[int,int]` but `Tuple` is not imported; safe only because `from __future__ import annotations` defers evaluation |
| F14 | Low | Config mismatch | `seed/config.json:population=500` vs `README`/`models.py`/`controller.py` default 25 | Documented population (25) disagrees with actual config (500) and infra comments (500) |
| F15 | Low | Non-determinism | `crime.py:__init__` default `Random(1337)`, `ticker` reseed rebuilds engines | Shared crime RNG across all agents; reseed creates a *new* `Crime_Engine` resetting the sequence |
| F16 | Low | Session-id / harness | `main.py:_invoke` | Session id padding can produce <33 chars for empty agentId edge; error path in `.decision` re-raises inside pool (fine) but plan/reflect/utterance swallow silently |

---

## High-severity findings

### F1 — Abandoned harness threads leak and can race on shared world state
**File:** `engine/village/ticker.py` (fan-out `_fan_out_decisions`, ~L300–345); comment at L2 / L318–345.

The fan-out deliberately calls `ex.shutdown(wait=False, cancel_futures=True)` after a `DECISION_BATCH_DEADLINE_SEC` (8s) deadline and abandons in-flight futures:

```python
ex = concurrent.futures.ThreadPoolExecutor(max_workers=DECISION_MAX_WORKERS)
...
except concurrent.futures.TimeoutError:
    for fut, aid in fut_to_id.items():
        results.setdefault(aid, None)
finally:
    ex.shutdown(wait=False, cancel_futures=True)  # abandons running threads
```

Two problems:

1. **Thread / socket / memory leak.** `cancel_futures=True` only cancels *queued* futures; futures already running keep their worker threads alive until the boto3 `read_timeout` (40s) elapses. With a slow/hung AgentCore Runtime, each tick (as fast as 1s of real time) can spawn up to 8 new workers while the previous 8 are still blocked → threads and their request buffers pile up far beyond `DECISION_MAX_WORKERS`. This is a genuine unbounded-concurrency path under exactly the failure mode the comment claims to defend against. There is no global cap on live executors/threads across ticks.

2. **Latent data race.** The design assumes results are applied on the main thread, but `self.runtime.decision(...)` runs on worker threads while the main thread continues into steps 4–7 of the *next* work (day rollover, law, conversations, persist) touching the same `world.agents`. The harness client itself only reads the request dict (built before submit), so today it is *mostly* read-only from the worker — but this is an implicit, undocumented invariant. Any future change that mutates agent state inside `decision()` (e.g. writing STM, stamping `lastDecisionAt`) becomes an unsynchronised read-modify-write against the main thread with no lock. There is no `threading.Lock` anywhere in the engine.

**Remediation:**
- Bound total in-flight harness work across ticks (e.g. a single long-lived `ThreadPoolExecutor` reused across ticks, or a semaphore counting live calls; refuse to submit new work while the previous batch's threads are still blocked).
- Rely on the boto3 `read_timeout` (already 40s) but **also** lower it toward the batch deadline so abandoned threads die quickly, and set `max_pool_connections` explicitly (default is 10; with 8 workers you are near the limit and a leaked batch will exhaust it — see F11).
- Document and enforce that `decision()` is pure/read-only, or guard shared state with a lock.

---

### F2 — ThreadPoolExecutor deadline semantics leave workers mutating state after the tick proceeds
**File:** `engine/village/ticker.py` L318–345.

`as_completed(fut_to_id, timeout=DECISION_BATCH_DEADLINE_SEC)` raises `TimeoutError` when the deadline passes, but the underlying futures are **not** cancelled or awaited — they continue executing (see F1). The code then returns heuristic fallbacks for undecided agents and the tick moves on. If a late future *does* complete, its result is silently discarded (the `results.setdefault(aid, None)` already fixed the value), which is benign for reads but wastes an LLM call that was still **charged** to the budget by the worker via `record_invocation` — except that charging happens in `_apply_decision`/`_record_usage` on the main thread, so a late/abandoned call's tokens are **never** recorded → **budget under-counts** abandoned invocations (compounds F3).

Also note `cancel_futures=` was added in Python 3.9. The Dockerfile targets 3.12 so this is fine in prod, but the tests ran under 3.9 pyc — verify the deployed base image actually is 3.12 (README says so; Dockerfile says `python:3.12-slim`). If any environment runs 3.8, `shutdown(wait=False, cancel_futures=True)` raises `TypeError` and every multi-agent tick throws.

**Remediation:** reuse one executor; on deadline, record that N calls were abandoned and bill them to the budget (or explicitly de-count them); pin the Python version in CI to match prod (3.12) so the pyc/version drift can't hide a `TypeError`.

---

### F3 — Budget guardrail can overshoot: check-before / count-after with parallel fan-out
**Files:** `engine/village/budget.py` L100–140 (`can_start_decision`, `record_invocation`); `ticker.py` L236–260 (fan-out gating), L292–300 (plan), L430 (reflect), `_record_usage` L?.

The gate is:

```python
if self.budget.can_start_decision():   # hour_invocations < cap
    pending.append(agent)
```

but the count is only incremented later, in `record_invocation`, which runs when the **response** is applied (`_record_usage`) — and only if the harness returned a `tokenUsage` block. Consequences:

1. **Cap overshoot under fan-out.** All `pending` agents (up to `DECISION_MAX_PER_TICK=8`) pass `can_start_decision()` in a tight loop **before any** invocation is counted. If only 1 slot remained under the cap, up to 8 calls are still launched. The cap is a soft advisory, not a hard limit.
2. **Uncounted invocations.** If the harness errors (returns `None`) or omits `tokenUsage` (e.g. the safe-idle error path in `harness.py:invocations`, which returns `tokenUsage: …,0,0`), the call is made and billed by AWS but the *invocation count* is only bumped when `tokenUsage` is present — the error path does include a zero-token usage block, but abandoned calls (F2) never reach `_record_usage`, so those invocations are entirely uncounted.
3. **Spend cap is post-hoc.** `spend_cap_reached()` flips `_paused_for_spend` **after** the over-limit invocation's cost is added, and the sim only pauses at the *end* of the tick (step 7). A single tick can dispatch 8 decisions + 4 conversations (each several serial LLM calls) + plans + reflections and blow well past `maxSpendUSD` before the pause fires.

**Remediation:** reserve budget atomically at gate time (increment a pending counter before submit, reconcile on completion), decrement on failure; make `can_start_decision()` account for in-flight reservations; check `spend_cap_reached()` between sub-phases within a tick, not only at the end.

---

### F4 — Unbounded synchronous DynamoDB work on the tick hot path
**File:** `engine/main.py` main loop (`refresh_injected_events` each iteration; `load_world` on reseed; reseed status `get` each iteration).

Every loop iteration:
- `store.get(sim_id, sk_status())` — one read (reseed check),
- `read_control(...)` — one read,
- `refresh_injected_events(...)` — a `query_pk(sim_id, sk_prefix="INJECTED_EVENT#")` that **paginates all** injected-event items,
- `ticker.advance_once(...)`,
- `write_status(...)` — one write.

The injected-event scan runs **every** iteration (~1s). The code comment correctly notes that scanning the whole partition would be catastrophic and scopes to `INJECTED_EVENT#`, but it still re-reads and re-parses the entire injected-event set every second forever; there is no "since" cursor. On reseed, `load_world` does a **full** `query_pk` (every AGENT#, LOC#, JOB#, CONFIG, INJECTED_EVENT#) synchronously on the tick thread — at 500 agents plus locations/jobs that is a multi-page scan blocking the clock. All of this is `time`-billed against `loop_interval`, and if it exceeds the interval the world clock silently falls behind real time (the clock advances by real elapsed, so ticks bunch — see F9).

**Remediation:** move control/reseed/injected-event polling off the tick cadence (e.g. every N seconds, or via a DynamoDB Streams / SQS notification); track a `lastSeenInjectedSeq` cursor and query `SK > cursor`; perform reseed `load_world` on a background thread and swap atomically.

---

### F5 — Per-agent buffers grow without a global bound; one is persisted forever
**Files:** `ticker.py` `self._stm` (STM mirror), `WorldState.processedEventIds`, `AgentState.creditedConversations` (`models.py:274`, serialized in `to_dict` L?).

- `self._stm[agentId]` is capped at `STM_LIMIT=30` per agent (good), but there are 500 agents and the dict is never pruned for departed/absent agents. Bounded but O(agents×30) permanently.
- `WorldState.processedEventIds` only grows; every injected event id stays forever. Fine for a demo, unbounded for a long run.
- **`AgentState.creditedConversations` is a `List[str]` that only ever appends** (`needs.on_conversation_complete` L?) and is written to DynamoDB **every tick** via `agent.to_dict()`. Over a multi-day run each agent accumulates one entry per conversation; the per-item DynamoDB payload grows monotonically, inflating every per-tick write (F6) and eventually risking the 400 KB item limit. It is never trimmed.

**Remediation:** bound `creditedConversations` (ring buffer, or store only recent ids / a bloom-style set with TTL); prune `_stm` and `processedEventIds` for agents no longer present or events past their TTL.

---

### F6 — Persistence write amplification: every agent written every tick
**File:** `ticker.py` `_process_tick` step 6:

```python
for agent in self.world.agents.values():
    agent.persistedSimTime = sim_iso
    self._persist(agent)          # store.put_item per agent, per tick
    persisted += 1
```

At 500 agents this is **500 `put_item` calls every sim-minute tick**, with no change detection and no `batch_writer`. Under accel the tick can fire multiple times per real second. This dominates the tick cost, is the most likely cause of the clock lag the code repeatedly guards against, and drives DynamoDB write-capacity/cost. `put_item` also retries synchronously (F7), so a single throttled agent write stalls the persist loop.

**Remediation:** persist only agents whose state changed this tick (dirty flag); use `table.batch_writer()` to batch the 500 writes into `BatchWriteItem` (25/req) with `boto3` auto-retry of unprocessed items; consider a periodic full-snapshot cadence instead of per-tick full writes.

---

## Medium-severity findings

### F7 — `DynamoStore` retry is over-broad, blocking, and inconsistent
**File:** `engine/village/state.py` `put` L?, `get`/`query`/`_paginated` (no retry).

```python
def put(self, item):
    attempts = 0
    while True:
        try:
            self.table.put_item(Item=item)
            return
        except Exception:                 # retries EVERYTHING
            if attempts >= WRITE_RETRIES: # 3
                raise
            self._sleep(RETRY_BACKOFF[attempts])  # 1s,2s,4s on the tick thread
            attempts += 1
```

- Retries on **any** `Exception`, including non-transient ones (`ValidationException`, `ConditionalCheckFailedException`, serialization errors) — wasting up to 7 seconds of blocking sleep on the single tick thread before finally raising.
- `get`, `query`, `_paginated` have **no** retry at all, so read throttling propagates immediately (inconsistent resilience posture vs writes).
- The boto3 resource is created with **default** config → default SDK retry mode (`legacy`, 3 attempts) layered *under* this manual retry, compounding latency. No `adaptive` retry mode is configured for DynamoDB.

**Remediation:** narrow retry to throttling/5xx (`ProvisionedThroughputExceededException`, `ThrottlingException`, `RequestLimitExceeded`, `InternalServerError`, `ServiceUnavailable`); add the same to reads; configure the boto3 client with `retries={"mode":"adaptive","max_attempts":…}` and rely on it rather than hand-rolled sleeps; never sleep on the tick thread (F11).

### F8 — Time / timezone correctness
**Files:** `law.py` `check_release`/`check_suspect_autoclear`, `economy.py` `shift_overlap_minutes`, `main.py` clock rehydrate.

- `law.check_release`: `release_dt = datetime.fromisoformat(st.detainedReleaseSimTime)` produces an **aware** datetime (the stored ISO has an offset), then compares `localize(sim_time) < release_dt`. `localize` re-normalises to Melbourne; if the stored offset was written during AEDT (+11) and the comparison happens during AEST (+10) or vice-versa across a DST boundary, the comparison is correct only because both are offset-aware — but `check_suspect_autoclear` does `localize(sim_time) - since` where `since = datetime.fromisoformat(st.suspectedSince)`. If any legacy record stored a **naive** timestamp, `fromisoformat` yields naive and the subtraction raises `TypeError`, aborting the (unguarded) law loop (F10).
- `economy.shift_overlap_minutes` computes overlap purely in minutes-of-day (`hour*60+minute`), so a shift that **crosses midnight** (e.g. `shiftStart=22:00`, `shiftDurationHours=8` → `shift_end=1800` minutes = 30:00) is compared against an `action_end` that never exceeds ~1440, silently truncating overnight-shift wages. `timeutil.is_open_at` handles overnight hours; the wage math does not.
- `main.py` rehydrate: `datetime.fromisoformat(config.startSimTime)` then `localize(...)`. `startSimTime` in config is `2026-03-02T06:00:00+11:00` (AEDT); `localize` re-normalises — fine — but a persisted `simTime` written pre-DST-change compared to post-change relies on `astimezone` correctness; acceptable, but the mix of "sometimes naive, sometimes aware" `fromisoformat` inputs across the codebase is fragile.

**Remediation:** centralise parsing through one helper that always returns an aware Melbourne datetime and rejects/repairs naive inputs; fix `shift_overlap_minutes` to handle `shift_end > 1440` (wrap or extend the action window); add tests for overnight shifts and a DST-boundary detention release.

### F9 — Clock: `_accrue` doesn't reconcile boundaries; unbounded catch-up tick list
**File:** `engine/village/clock.py` `advance` L?, `_accrue` L?.

- `_accrue` (called on pause / `set_acceleration`) advances `_sim_time` but leaves `_last_boundary` unchanged and comments "Re-emit boundaries lazily on next advance()". That means the **next** `advance` will emit *all* minute boundaries between the old `_last_boundary` and the accrued `_sim_time` at once. If a long real interval elapsed during a paused/accel-change window, the subsequent `advance` produces a large `ticks` list.
- More generally, `advance` builds `ticks` with an unbounded `while boundary + 1min <= sim_time` loop. If the tick loop ever stalls for a long real interval (e.g. a 40s hung harness batch at accel=60 → 40×60/60 = 40 sim-minutes → 40 ticks in one `advance`, each running the full 500-agent pipeline), the engine tries to process the entire backlog in a single `advance_once`, which takes even longer, and the world clock falls further behind — a positive-feedback stall. The lag-warning is emitted but nothing sheds load.
- `advance_once` runs **all** ticks from one `advance()` result in a loop with no per-iteration time budget, so a backlog cannot be spread across loop iterations.

**Remediation:** cap ticks processed per `advance_once` (process at most K, carry the rest); or coalesce needs-decay across skipped minutes; reset `_last_boundary` in `_accrue`; add a max-backlog guard that fast-forwards state instead of replaying every minute.

### F10 — A single failing agent aborts the whole tick in several phases
**File:** `ticker.py` `_process_tick` — steps 1 (needs), 4 (day rollover), 5 (law), 6 (persist), plus `_run_conversations` outer loop.

`_on_action_complete` is carefully wrapped in `try/except` per agent (good). But the needs-decay loop, the day-rollover economy loop, the law loop, and the persist loop iterate `self.world.agents.values()` with **no** per-agent guard. Any exception — a malformed `AgentState`, a `TypeError` from F8's naive datetime, a `KeyError` on a missing home/job, a Decimal/float surprise in `apply_daily_living_cost` — propagates out of `_process_tick`, is caught only by the outer `try` in `main.py`, and **the entire tick is lost for all 500 agents** (no persistence, no events for that minute). The generic `except Exception` in `main.py` prevents a crash but converts one bad agent into a world-wide skipped tick every time that agent is processed → the sim can wedge on one poisoned record.

**Remediation:** wrap each per-agent body in the needs/rollover/law/persist loops in `try/except` that logs and continues (matching the discipline already used in `_on_action_complete` and `_apply_heuristic_decision`).

### F11 — Blocking sleeps and network I/O on the single tick thread
**Files:** `main.py` main loop; `state.py` `put` (`RETRY_BACKOFF` sleeps); `budget.py` `invoke_with_retry` (sleeps); harness calls.

Everything runs on one thread. The DynamoDB write retry sleeps up to 7s (F7); the budget/harness retry sleeps up to ~30s×5 (F7/F3); the reseed `load_world` and per-tick injected-event scan are synchronous (F4); the harness decision fan-out is parallel but plan/reflect/utterance are **serial synchronous** harness calls inside the tick (`_trigger_plan`, `_trigger_reflect`, `_run_conversations` each call `self.runtime.*` directly and can block for the full 40s read-timeout each). A conversation runs up to 12 serial utterance calls — at 40s timeout that is potentially minutes inside one tick with the clock frozen.

**Remediation:** never sleep on the tick thread; move all harness/DynamoDB retry+backoff onto worker threads or an async task; cap conversation LLM time with a hard wall-clock deadline like the decision fan-out has; treat plan/reflect/utterance with the same bounded-concurrency + deadline discipline as decisions.

### F12 — Invocation cap resets per **sim** hour, not real hour
**File:** `budget.py` `on_tick` / `_hour_key` (`sim_time_iso[:13]`).

`maxInvocationsPerSimHour` is keyed on the **simulated** hour. At `accelerationFactor=4`, one sim hour = 15 real minutes; at accel=60, one sim hour = 1 real minute. So the "per hour" invocation cap actually resets every real minute at high accel, allowing 60× the intended real-world Bedrock call rate. As a *rate limiter protecting real spend and real Bedrock quotas*, this is materially weaker than it appears and scales inversely with accel.

**Remediation:** if the intent is protecting real Bedrock quota/cost, key the cap on **real** wall-clock hour (or both). Document explicitly which clock governs the cap; make it independent of `accelerationFactor`.

---

## Low-severity findings

### F13 — `Tuple` used in an annotation but not imported
**File:** `ticker.py` L17 import (`Any, Callable, Dict, List, Optional, Protocol`) vs L614 `def _conversation_turn_band(self, convo) -> Tuple[int, int]:`.
Only harmless because `from __future__ import annotations` (L?) turns annotations into strings that are never evaluated at runtime. It **will** raise `NameError` if anyone calls `typing.get_type_hints(Ticker._conversation_turn_band)` (docs tooling, pydantic, some serializers). **Remediation:** add `Tuple` to the import.

### F14 — Population config mismatch (25 vs 500)
`seed/config.json` sets `"population": 500`. The `README.md` and `seed/README.md` say 25; `models.Config.population` and `controller.POPULATION_DEFAULT` default to 25; git history and `infra/lib/village-stack.ts` (L477 comment, 4 vCPU/8 GB sizing) confirm the **intended** scale is 500. So the *running* config is 500 while documentation and code defaults still say 25. This is a documentation/consistency defect (the task brief expected 25; the actual `config.json` is 500). Given the engine's per-tick full-persist and synchronous harness paths (F4/F6/F11), 500 is the scale at which the resilience issues above actually bite. **Remediation:** reconcile the docs to state 500; confirm 500 is intended and load-tested; note the engine currently mitigates 500 by defaulting `DECISION_MAX_PER_TICK=8` + heuristic fallback, i.e. at 500 agents the **vast majority never call the LLM** on a given tick (only 8 decisions + ≤4 conversations per tick), which is a significant behavioural limitation worth documenting.

### F15 — Shared crime RNG + reseed resets determinism
**File:** `crime.py` `__init__` default `random.Random(1337)`; `main.py`/`ticker` reseed rebuilds `Crime_Engine()`.
A single `Random(1337)` is shared by all agents' crime rolls (fine for determinism, but the sequence is order-dependent on agent iteration). On reseed the engine constructs a fresh `Crime_Engine`, resetting the RNG to seed 1337 — so post-reseed crime outcomes are correlated with pre-reseed ones. Low impact for a demo. **Remediation:** derive per-crime seeds from `(eventId/agentId, simTime)` for reproducibility independent of iteration order and reseed.

### F16 — Harness invoke edge cases
**File:** `main.py` `BedrockAgentRuntimeClient._invoke` / `.decision`/`.plan`/`.reflect`/`.utterance`.
- Session id: `raw + "-" + "0"*33` then `[:64]`; a second guard pads to ≥33 — correct, but the double-truncate is convoluted and could be simplified.
- `.decision` logs and **re-raises** (so the pool captures it — correct). `.plan`/`.reflect`/`.utterance` do **not** catch at this layer; they raise into the callers, which *do* guard (`_trigger_plan` etc. wrap in try/except) — so consistent, but the asymmetry is easy to misread.
- The harness `invocations` handler returns a safe idle action on **any** exception for `op=decision` (good), but returns HTTP 500 for other ops; the engine treats a 500 as an exception and falls back — acceptable.

---

## What was verified vs. not

- **Verified by reading source:** all control-flow and data-flow claims above (fan-out shutdown semantics, budget check/count ordering, per-tick persist loop, unguarded per-agent loops, `Tuple` import absence, `population:500` in `config.json`, overnight-shift minute math, `_accrue` boundary handling, per-sim-hour budget key).
- **Verified externally:** botocore default `max_pool_connections=10` (AWS botocore config reference); boto3 **clients** are generally thread-safe while **resources**/sessions are not (`DynamoStore` uses a resource `.Table` but only from the main thread — OK); `ThreadPoolExecutor.shutdown(cancel_futures=)` is Py3.9+.
- **Not verified (no runtime access):** actual tick wall-time at 500 agents, real DynamoDB throttling behaviour, live Bedrock latency, whether the deployed image is genuinely 3.12 (Dockerfile says so; local pyc are 3.9). Load-testing at 500 agents is the single most valuable next step to confirm F4/F6/F9/F11 severity.

## Recommended priority order

1. **F6 + F4 + F11** (the clock-stall triad) — dirty-flag + `batch_writer` persistence, move polling off the hot path, no blocking sleeps on the tick thread.
2. **F3 + F1/F2** — make the budget a hard reservation and bound cross-tick harness threads.
3. **F10** — per-agent guards so one poisoned record can't skip world ticks.
4. **F8 + F12 + F9** — time/DST correctness, real-hour budget key, bounded catch-up.
5. **F5, F7, F13–F16** — cleanup and long-run memory hygiene.

## Deferred (higher-risk) remediations

The following findings are **tracked but deferred** from the current safe pass:
they require larger architectural changes with real runtime/behavioural risk
(concurrency model, persistence path, budget accounting) and should be tackled
deliberately with load-testing at 500 agents, not folded into a low-risk fix.

- **F1 — Abandoned harness threads leak/race.** Needs a long-lived executor or
  live-call semaphore to bound cross-tick concurrency; a locking/read-only
  contract change on `decision()`. Risk: reworks the fan-out concurrency model.
- **F2 — Executor deadline leaves workers mutating state / Py-version guard.**
  Tied to F1's executor rework plus abandoned-call budget reconciliation.
- **F3 — Budget overshoot (check-before/count-after).** Requires atomic
  reserve-at-gate + reconcile-on-completion accounting across the parallel
  fan-out and sub-phase spend checks; changes budget semantics.
- **F4 — Synchronous DynamoDB polling/reseed on the hot path.** Needs polling
  moved off the tick cadence (timer/Streams/SQS) and a "since" cursor; touches
  the main-loop I/O architecture.
- **F5 — Unbounded per-agent buffers (`creditedConversations` persisted
  forever).** Requires a ring-buffer/TTL bound and a persistence-format change;
  behavioural (idempotency) implications for social crediting.
- **F6 — Per-tick full-persist write amplification.** Needs dirty-flag change
  detection + `batch_writer` batching; reworks the persistence path (the single
  highest-value but highest-risk change).
- **F9 — Clock catch-up / `_accrue` boundary reconciliation.** Requires
  capping ticks per `advance_once` and boundary reset logic; changes clock
  timing behaviour and needs careful backlog tests.
- **F11 — Blocking sleeps/network I/O on the tick thread.** Coupled to F1/F4/F7;
  moving retry/backoff and conversation LLM time off the tick thread is an
  async/threading rework.
- **F12 — Invocation cap keyed on sim hour, not real hour.** Semantically
  changes the rate limiter (real-clock keying); needs product decision on
  intended behaviour before changing guardrail semantics.
