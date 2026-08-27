"""Fargate entrypoint for the Melbourne Agent Village simulation engine.

Constructs the tick loop from environment configuration and runs it as a
long-running process, polling the DynamoDB CONTROL item each loop iteration to
apply start/pause/resume/stop commands, and writing STATUS back.

Environment:
  TABLE_NAME          DynamoDB table (default "village")
  SIM_ID              simulation id (required)
  AGENT_RUNTIME_ARN   AgentCore Runtime ARN for the harness
  AWS_REGION          AWS region (default ap-southeast-2)
  MEMORY_ID           AgentCore Memory id
  LOOP_INTERVAL_SEC   real seconds between loop iterations (default 1.0)

This module performs live AWS I/O; the pure domain library under village/ is
fully unit-tested independently of AWS.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from village.budget import Budget_Accountant
from village.clock import Simulation_Clock, localize
from village.controller import Simulation_Controller
from village.crime import Crime_Engine
from village.economy import Economy_Engine
from village.eventlog import Event_Log, seq20
from village.events_inject import Injected_Event
from village.law import Law_Enforcement_Engine
from village.models import Agent, Config, Job, Location, SimStatus
from village.movement import Movement_Engine
from village.social import Social_Engine
from village.state import (DynamoStore, World_State_Parser,
                           World_State_Serializer, sk_control, sk_status)
from village.ticker import Ticker, WorldState


class BedrockAgentRuntimeClient:
    """Real AgentRuntimeClient backed by bedrock-agentcore invoke_agent_runtime.

    Kept minimal here; the full harness contract lives in app/harness. This
    client marshals requests and unwraps structured responses.
    """

    def __init__(self, runtime_arn: str, region: str,
                 budget: Optional[Budget_Accountant] = None):
        import boto3
        from botocore.config import Config as BotoConfig
        # Per-call network timeouts live on the client (not the executor) so a
        # hung harness invocation aborts the socket and frees the worker thread
        # rather than pinning one of the bounded decision-pool slots. Modest
        # adaptive retries smooth over transient AgentCore throttling.
        cfg = BotoConfig(
            connect_timeout=5,
            read_timeout=40,
            retries={"max_attempts": 2, "mode": "adaptive"},
        )
        self._client = boto3.client("bedrock-agentcore", region_name=region,
                                    config=cfg)
        self._arn = runtime_arn
        self._budget = budget

    def _invoke(self, req: Dict[str, Any]) -> Dict[str, Any]:
        # AgentCore requires runtimeSessionId of 33-256 chars. Build a stable
        # per-agent-per-day session id and pad to the minimum length.
        raw = f"{req.get('simId','melb')}-{req.get('agentId','')}-{req.get('simTime','')[:10]}"
        session = (raw + "-" + ("0" * 33))[:64]
        if len(session) < 33:
            session = (session + ("0" * 33))[:33]
        resp = self._client.invoke_agent_runtime(
            agentRuntimeArn=self._arn,
            runtimeSessionId=session,
            payload=json.dumps(req).encode("utf-8"),
        )
        body = resp.get("response") or resp.get("payload")
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        return json.loads(body) if isinstance(body, str) else (body or {})

    def decision(self, request):
        try:
            return self._invoke({**request, "op": "decision"})
        except Exception as e:  # noqa: BLE001
            print(f"[engine] harness decision error agent={request.get('agentId')}: {e}", flush=True)
            raise
    def plan(self, request): return self._invoke({**request, "op": "plan"})
    def reflect(self, request): return self._invoke({**request, "op": "reflect"})
    def utterance(self, request): return self._invoke({**request, "op": "utterance"})


def load_world(store: DynamoStore, parser: World_State_Parser,
               sim_id: str) -> WorldState:
    items = store.query_pk(sim_id)
    config: Optional[Config] = None
    agents: Dict[str, Agent] = {}
    locations: Dict[str, Location] = {}
    jobs: Dict[str, Job] = {}
    injected: Dict[str, Injected_Event] = {}
    for item in items:
        sk = item.get("SK", "")
        try:
            if sk == "CONFIG":
                config = parser.parse(item)
            elif sk.startswith("AGENT#"):
                a = parser.parse(item); agents[a.id] = a
            elif sk.startswith("LOC#"):
                loc = parser.parse(item); locations[loc.id] = loc
            elif sk.startswith("JOB#"):
                j = parser.parse(item); jobs[j.id] = j
            elif sk.startswith("INJECTED_EVENT#"):
                # Injected events are raw items (not part of the domain state
                # parser). Fall back to the SK for the id if none is present.
                ev = Injected_Event.from_dict({**item, "id": item.get("id") or sk})
                if ev.id:
                    injected[ev.id] = ev
        except Exception:
            continue
    if config is None:
        config = Config(simId=sim_id)
    return WorldState(config=config, agents=agents, locations=locations,
                      jobs=jobs, injectedEvents=injected)


def write_status(store: DynamoStore, sim_id: str, status: SimStatus,
                 sim_time_iso: str, accel: int) -> None:
    store.put({
        "PK": f"SIM#{sim_id}", "SK": sk_status(),
        "status": status.value, "simTime": sim_time_iso,
        "updatedAt": datetime.now().astimezone().isoformat(),
        "accel": accel, "schemaVersion": 1,
    })


def read_control(store: DynamoStore, sim_id: str) -> Optional[Dict[str, Any]]:
    return store.get(sim_id, sk_control())


def refresh_injected_events(store: DynamoStore, world: WorldState,
                            sim_id: str) -> int:
    """Merge any newly-injected events from DynamoDB into the live world.

    Operators inject events while the sim runs, so we re-scan for
    ``INJECTED_EVENT#`` items each loop and add any not already known. Already
    processed ids are left in ``processedEventIds`` so they don't re-fire.
    Returns the number of newly discovered events. Defensive: never raises.
    """
    added = 0
    try:
        items = store.query_pk(sim_id)
    except Exception as e:  # noqa: BLE001
        print(f"[engine] injected-event refresh error: {e}", flush=True)
        return 0
    for item in items:
        sk = item.get("SK", "")
        if not sk.startswith("INJECTED_EVENT#"):
            continue
        try:
            ev = Injected_Event.from_dict({**item, "id": item.get("id") or sk})
        except Exception:  # noqa: BLE001
            continue
        if ev.id and ev.id not in world.injectedEvents:
            world.injectedEvents[ev.id] = ev
            added += 1
    return added


class DynamoEventSink:
    """Event sink that persists every appended Event_Log entry to DynamoDB so
    the Simulation_API can query events / decision-trails / summaries (Req 14).

    Keeps a bounded in-memory mirror so in-process queries still work.
    """

    def __init__(self, store: DynamoStore, serializer: World_State_Serializer):
        self._store = store
        self._serializer = serializer
        self._mirror: list = []

    def append(self, entry) -> None:  # EventLogEntry
        self._mirror.append(entry)
        try:
            self._store.put(self._serializer.event(entry))
        except Exception as e:  # noqa: BLE001
            print(f"[engine] event persist error seq={getattr(entry,'seq','?')}: {e}",
                  flush=True)

    def all(self):
        return list(self._mirror)


def main() -> None:
    table_name = os.environ.get("TABLE_NAME", "village")
    sim_id = os.environ["SIM_ID"]
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    runtime_arn = os.environ.get("AGENT_RUNTIME_ARN", "")
    loop_interval = float(os.environ.get("LOOP_INTERVAL_SEC", "1.0"))

    store = DynamoStore(table_name=table_name, region=region)
    parser = World_State_Parser()
    serializer = World_State_Serializer(sim_id)

    world = load_world(store, parser, sim_id)
    config = world.config

    # Resume the simulated clock from the last persisted STATUS.simTime rather
    # than always resetting to config.startSimTime. Without this, every engine
    # restart / Fargate task replacement (e.g. a redeploy) snapped sim time back
    # to the world's start, so the global clock appeared "stuck" near the start.
    # We rehydrate the last known sim time (and status) so the clock advances
    # continuously across process restarts.
    persisted_status = store.get(sim_id, sk_status()) or {}
    persisted_sim_time = persisted_status.get("simTime")
    persisted_state = (persisted_status.get("status") or "").strip().lower()
    start_sim_dt = localize(datetime.fromisoformat(config.startSimTime))
    if persisted_sim_time:
        try:
            start_sim_dt = localize(datetime.fromisoformat(persisted_sim_time))
        except (TypeError, ValueError):
            start_sim_dt = localize(datetime.fromisoformat(config.startSimTime))

    clock = Simulation_Clock(
        start_sim_time=start_sim_dt,
        acceleration_factor=config.accelerationFactor,
        real_clock=time.monotonic,
    )
    controller = Simulation_Controller(config)
    # If the world was RUNNING before this process (re)started, resume running
    # so a redeploy doesn't silently freeze the world at its last tick.
    if persisted_state == SimStatus.RUNNING.value:
        try:
            controller.apply("start", has_persisted_state=bool(world.agents))
        except Exception:  # noqa: BLE001 — never block startup
            pass
    budget = Budget_Accountant(config.budget)
    event_log = Event_Log(sink=DynamoEventSink(store, serializer), start_seq=0)
    runtime = BedrockAgentRuntimeClient(runtime_arn, region, budget) if runtime_arn else None

    detention = world.locations.get(config.detentionFacilityId) if config.detentionFacilityId else None
    law = None
    if detention is not None:
        social_ref = Social_Engine()

        def sentiment_lookup(witness_id: str, perp_id: str) -> int:
            return social_ref.get_relationship(witness_id, perp_id).sentiment

        law = Law_Enforcement_Engine(detention, sentiment_lookup)

    def persist(agent: Agent) -> None:
        store.put(serializer.agent(agent))

    ticker = Ticker(
        world=world, clock=clock, controller=controller,
        runtime=runtime, event_log=event_log, budget=budget,
        movement=Movement_Engine(list(world.locations.values())),
        economy=Economy_Engine(), social=Social_Engine(),
        crime=Crime_Engine(), law=law, persist=persist,
    )

    last_nonce: Optional[str] = None
    print(f"[engine] started sim={sim_id} table={table_name} region={region}", flush=True)

    while True:
        loop_start = time.monotonic()
        # poll CONTROL item (<=1s per DESIGN §2)
        try:
            ctrl = read_control(store, sim_id)
            if ctrl and ctrl.get("nonce") != last_nonce:
                last_nonce = ctrl.get("nonce")
                cmd = ctrl.get("command")
                if cmd == "start":
                    controller.apply("start", has_persisted_state=bool(world.agents))
                    if controller.status == SimStatus.RUNNING:
                        clock.resume()
                elif cmd == "pause":
                    controller.apply("pause"); clock.pause()
                elif cmd == "resume":
                    controller.apply("resume"); clock.resume()
                elif cmd == "stop":
                    controller.apply("stop")
        except Exception as e:  # noqa: BLE001
            print(f"[engine] control poll error: {e}", flush=True)

        try:
            proc = time.monotonic() - loop_start
            refresh_injected_events(store, world, sim_id)
            report = ticker.advance_once(tick_processing_seconds=proc)
            if report is not None and (report.decisions_triggered or report.events_written):
                print(f"[engine] tick {report.sim_time} decisions={report.decisions_triggered} "
                      f"throttled={report.decisions_throttled} events={report.events_written} "
                      f"persisted={report.persisted}", flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[engine] tick error: {e}\n{traceback.format_exc()}", flush=True)

        try:
            write_status(store, sim_id, controller.status,
                         clock.sim_time_iso(), clock.acceleration_factor)
        except Exception:
            pass

        elapsed = time.monotonic() - loop_start
        if elapsed < loop_interval:
            time.sleep(loop_interval - elapsed)


if __name__ == "__main__":
    main()
