"""Ticker — the main tick loop wiring all engines (DESIGN.md §8).

Per tick (1 sim minute):
  advance clock -> needs decay/recover -> movement progress -> end finished
  actions -> trigger decision cycles (via injected AgentRuntimeClient) ->
  resolve conversations -> resolve crimes -> law enforcement -> economy day
  rollover -> persist changed state -> write events -> budget gating.

The AgentRuntimeClient is a Protocol so the harness call is injectable and the
loop is unit-testable with a fake.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol

from .budget import Budget_Accountant
from .clock import DayRollover, Simulation_Clock, Tick
from .controller import Simulation_Controller
from .crime import Crime_Engine
from .economy import Economy_Engine
from .eventlog import Event_Log
from .events_inject import (Event_Propagation, Injected_Event,
                            add_sim_minutes, AVOID_TTL_MIN, ATTRACT_TTL_MIN)
from .heuristics import heuristic_decision, local_utterance
from .law import Law_Enforcement_Engine
from .models import (Action, ActionType, Agent, Config, EmploymentStatus, Job,
                     LegalStatus, Location, SimStatus, TargetType)
from .movement import Movement_Engine, haversine_m
from .needs import (apply_decay_tick, apply_energy_recovery_tick,
                    update_critical_flags)
from .social import Social_Engine

# Bounded concurrency for per-agent harness calls within a tick. Kept small to
# respect the invocation budget and the AgentCore Runtime; network I/O bound so
# a modest pool keeps 25 agents responsive without saturating the engine task.
DECISION_MAX_WORKERS = 8
# Best-effort ceiling on how long we wait for the whole decision fan-out in a
# single tick before proceeding (real per-call timeouts live in the boto3
# client config on the engine side).
DECISION_BATCH_DEADLINE_SEC = 45.0
# How many nearest reachable locations to surface to the harness (DESIGN §6).
REACHABLE_LIMIT = 20
# How many short-term memory lines to mirror into a decision/utterance request.
STM_LIMIT = 30
# Needs at/below this fraction are surfaced as "critical" perception hints.
CRITICAL_NEED_THRESHOLD = 25


# --------------------------------------------------------------------------
# Agent runtime client interface (engine -> harness)
# --------------------------------------------------------------------------
class AgentRuntimeClient(Protocol):
    """Injectable interface to the Agent Harness (AgentCore Runtime).

    All methods return already-parsed structured responses. Implementations
    handle Bedrock invocation, retries, and Memory I/O. The engine validates
    the results (Req 6.5) and records budget/events.
    """

    def decision(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Return {"action": {...}} for op=decision."""
        ...

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Return {"plan": [ {...}, ... 3..12 ]} for op=plan."""
        ...

    def reflect(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Return {"reflections": [ {...}, ... 1..5 ]} for op=reflect."""
        ...

    def utterance(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Return {"utterance": "..."} for op=utterance."""
        ...


@dataclass
class WorldState:
    """Authoritative in-memory world held by the engine."""
    config: Config
    agents: Dict[str, Agent] = field(default_factory=dict)
    locations: Dict[str, Location] = field(default_factory=dict)
    jobs: Dict[str, Job] = field(default_factory=dict)
    # Operator-injected world events (explosion/festival/…), keyed by event id.
    # Populated from DynamoDB items with SK ``INJECTED_EVENT#<seq>``.
    injectedEvents: Dict[str, "Injected_Event"] = field(default_factory=dict)
    # Ids of injected events already processed by the propagation engine, so we
    # only fan each event out to the population once.
    processedEventIds: set = field(default_factory=set)

    def occupancy(self, location_id: str, radius_m: float = 25.0) -> int:
        """Count agents within radius_m of the location (Req 3.5)."""
        from .movement import haversine_m
        loc = self.locations.get(location_id)
        if loc is None:
            return 0
        count = 0
        for a in self.agents.values():
            if haversine_m(a.state.lat, a.state.lon, loc.lat, loc.lon) <= radius_m:
                count += 1
        return count


@dataclass
class TickReport:
    sim_time: str
    tick_count: int
    decisions_triggered: int
    decisions_throttled: int
    day_rollover: bool
    persisted: int
    events_written: int


class Ticker:
    """Drives the simulation loop. Deterministic given injected clients/clocks."""

    def __init__(self, world: WorldState, clock: Simulation_Clock,
                 controller: Simulation_Controller,
                 runtime: AgentRuntimeClient,
                 event_log: Event_Log,
                 budget: Budget_Accountant,
                 movement: Optional[Movement_Engine] = None,
                 economy: Optional[Economy_Engine] = None,
                 social: Optional[Social_Engine] = None,
                 crime: Optional[Crime_Engine] = None,
                 law: Optional[Law_Enforcement_Engine] = None,
                 persist: Optional[Callable[[Agent], None]] = None):
        self.world = world
        self.clock = clock
        self.controller = controller
        self.runtime = runtime
        self.event_log = event_log
        self.budget = budget
        self.movement = movement or Movement_Engine(list(world.locations.values()))
        self.economy = economy or Economy_Engine()
        self.social = social or Social_Engine()
        self.crime = crime or Crime_Engine()
        self.law = law
        self._persist = persist or (lambda a: None)
        # Injected-event propagation engine (deterministic, pure).
        self.propagation = Event_Propagation()
        # Short-term memory mirror: agentId -> recent memory lines (strings).
        # Gives the harness continuity of thought across ticks even when
        # AgentCore Memory is unavailable/degraded. Bounded per agent.
        self._stm: Dict[str, List[str]] = {}
        # Track which agents have already produced a day-plan / reflection for
        # the current sim date, so we emit them once per day (thought logging).
        self._planned_on: Dict[str, str] = {}
        self._reflected_on: Dict[str, str] = {}

    # -- one loop iteration -------------------------------------------------
    def advance_once(self, tick_processing_seconds: float = 0.0) -> Optional[TickReport]:
        """Advance the clock and process any crossed ticks. Returns a report."""
        if self.controller.status != SimStatus.RUNNING:
            return None
        result = self.clock.advance(tick_processing_seconds)
        if result.lag_warning is not None:
            self.event_log.append(
                self.clock.sim_time_iso(), "clock",
                f"lag warning: backlog {result.lag_warning.backlog_ticks} ticks",
                detail={"kind": "lag-warning",
                        "backlog": result.lag_warning.backlog_ticks})
        report: Optional[TickReport] = None
        for tick in result.ticks:
            report = self._process_tick(tick, result.day_rollovers)
        return report

    # -- process a single sim-minute tick ----------------------------------
    def _process_tick(self, tick: Tick, day_rollovers: List[DayRollover]) -> TickReport:
        sim_iso = tick.sim_time.replace(microsecond=0).isoformat()
        self.budget.on_tick(sim_iso)

        decisions = 0
        throttled = 0
        events_before = self.event_log.current_seq

        # 1. needs decay / recover
        for agent in self.world.agents.values():
            act = agent.state.currentAction
            sleeping = act is not None and act.type == ActionType.SLEEP
            if sleeping:
                apply_energy_recovery_tick(agent.state, self.world.config.energyRecoveryRate)
            else:
                apply_decay_tick(agent.state, self.world.config.decayRates)
            update_critical_flags(agent.state)

        # 2. movement progress + 3. end finished actions -> decision cycles
        for agent in self.world.agents.values():
            self._progress_action(agent, tick)

        # 2b. process any NEW injected world events once (explosion/festival/…):
        # propagate awareness, push memories, and set avoidance/attraction hints
        # BEFORE decisions so behaviour reacts this same tick.
        self._process_injected_events(sim_iso)

        # 3a. day-plan on wake / new day (emits a `planning` thought event).
        sim_date = sim_iso[:10]
        for agent in self.world.agents.values():
            if self._should_plan(agent, sim_iso, sim_date):
                if self.budget.can_start_decision():
                    self._trigger_plan(agent, sim_iso)
                self._planned_on[agent.id] = sim_date

        # 3b. fan out decision cycles for idle agents with bounded concurrency.
        # Building perception + calling the harness is network-I/O bound, so we
        # parallelise the calls (<=8 in flight) and then apply results on the
        # main thread to keep the rest of the tick deterministic.
        idle_agents = [a for a in self.world.agents.values()
                       if a.state.currentAction is None]
        pending: List[Agent] = []
        for agent in idle_agents:
            if self.budget.can_start_decision():
                pending.append(agent)
            else:
                throttled += 1
                self.event_log.append(
                    sim_iso, "model",
                    f"decision throttled for {agent.id}",
                    agents=[agent.id],
                    detail={"kind": "throttled"})
        for agent, resp in self._fan_out_decisions(pending, sim_iso):
            self._apply_decision(agent, resp, sim_iso)
            decisions += 1

        # 4. day rollover economy (Req 9.7)
        rolled = False
        for _ in day_rollovers:
            rolled = True
            for agent in self.world.agents.values():
                paid, unpaid = self.economy.apply_daily_living_cost(agent.state)
                if unpaid > 0:
                    self.event_log.append(
                        sim_iso, "employment",
                        f"unpaid living cost {unpaid:.2f} for {agent.id}",
                        agents=[agent.id],
                        detail={"kind": "unpaid-living-cost", "unpaid": unpaid})
            # End-of-day reflection: durable "thoughts" drawn from the day's
            # short-term memory. Emitted as `memory` category events so the SPA
            # and decision-trail surface each agent's reasoning (Req 7 / 14).
            for agent in self.world.agents.values():
                if self.budget.can_start_decision():
                    self._trigger_reflect(agent, sim_iso)
                self._reflected_on[agent.id] = sim_iso[:10]

        # 5. law: release + suspect auto-clear
        if self.law is not None:
            for agent in self.world.agents.values():
                home = self.world.locations.get(agent.persona.homeLocationId)
                job_exists = agent.state.jobId in self.world.jobs if agent.state.jobId else False
                if home is not None:
                    rel = self.law.check_release(agent, tick.sim_time, home, job_exists)
                    if rel is not None:
                        for desc in rel.events:
                            self.event_log.append(sim_iso, "legal", desc, agents=[agent.id])
                ac = self.law.check_suspect_autoclear(agent, tick.sim_time)
                if ac is not None:
                    for desc in ac.events:
                        self.event_log.append(sim_iso, "legal", desc, agents=[agent.id])

        # 5b. run & resolve co-located conversations (Req 10). Utterance text
        # is produced by the harness; the full transcript is persisted so the
        # SPA can display agent-to-agent conversations.
        self._run_conversations(sim_iso)

        # 6. persist changed agents
        persisted = 0
        for agent in self.world.agents.values():
            agent.persistedSimTime = sim_iso
            self._persist(agent)
            persisted += 1

        events_written = self.event_log.current_seq - events_before

        # 7. budget spend-cap pause (Req 18.6)
        if self.budget.spend_cap_reached() and self.controller.status == SimStatus.RUNNING:
            self.controller.pause_for_spend()
            self.event_log.append(
                sim_iso, "system",
                f"paused: spend cap {self.budget.total_spend:.2f} USD reached",
                detail={"kind": "spend-cap-pause", "spend": self.budget.total_spend})

        return TickReport(
            sim_time=sim_iso, tick_count=1, decisions_triggered=decisions,
            decisions_throttled=throttled, day_rollover=rolled,
            persisted=persisted, events_written=events_written)

    # -- helpers ------------------------------------------------------------
    def _progress_action(self, agent: Agent, tick: Tick) -> None:
        act = agent.state.currentAction
        if act is None:
            return
        if act.type == ActionType.TRAVEL and act.route:
            # advance progress by 1 minute of the expected duration
            act.progress = min(1.0, act.progress + 1.0 / max(1, act.expectedDurationMin))
            # interpolation handled by Movement_Engine when route present
            if act.progress >= 1.0:
                dest = self.world.locations.get(act.targetId)
                if dest is not None:
                    agent.state.lat = dest.lat
                    agent.state.lon = dest.lon
                    agent.state.presentLocationId = dest.id
                agent.state.currentAction = None
        else:
            act.progress = min(1.0, act.progress + 1.0 / max(1, act.expectedDurationMin))
            if act.progress >= 1.0:
                agent.state.currentAction = None

    def _trigger_decision(self, agent: Agent, sim_iso: str) -> None:
        """Build full perception, call the harness, and apply the result.

        Retained as a single-agent convenience (used by tests); the tick loop
        uses the parallel fan-out path below.
        """
        resp = self._call_decision(agent, sim_iso)
        self._apply_decision(agent, resp, sim_iso)

    # -- perception --------------------------------------------------------
    def _reachable_for(self, agent: Agent) -> List[Dict[str, Any]]:
        """Nearest REACHABLE_LIMIT locations with capacity + travel estimate.

        Ordered by straight-line travel time so the harness can pick concrete,
        reachable targets (enabling travel/work/eat/shop/leisure instead of
        idling for lack of options — DESIGN §6).
        """
        st = agent.state
        occupancy: Dict[str, int] = {}
        for other in self.world.agents.values():
            pid = other.state.presentLocationId
            if pid:
                occupancy[pid] = occupancy.get(pid, 0) + 1
        scored: List[tuple] = []
        for loc in self.world.locations.values():
            dist_km = haversine_m(st.lat, st.lon, loc.lat, loc.lon) / 1000.0
            # rough travel minutes at walking pace as an ordering key/estimate.
            travel_min = max(1, int(round(dist_km / 5.0 * 60.0)))
            remaining = max(0, loc.capacity - occupancy.get(loc.id, 0))
            scored.append((travel_min, loc, remaining))
        scored.sort(key=lambda t: t[0])
        out: List[Dict[str, Any]] = []
        for travel_min, loc, remaining in scored[:REACHABLE_LIMIT]:
            out.append({
                "id": loc.id, "name": loc.name, "category": loc.category.value,
                "remainingCapacity": remaining, "travelMin": travel_min,
                "price": loc.price,
            })
        return out

    def _colocated_for(self, agent: Agent) -> List[Dict[str, Any]]:
        """Other agents at the same present location (targetable for social)."""
        loc = agent.state.presentLocationId
        if not loc:
            return []
        out: List[Dict[str, Any]] = []
        for other in self.world.agents.values():
            if other.id == agent.id:
                continue
            if other.state.presentLocationId != loc:
                continue
            act = other.state.currentAction
            out.append({
                "id": other.id, "name": other.persona.name,
                "actionType": act.type.value if act else "idle",
            })
        return out

    def _perception_flags(self, agent: Agent) -> Dict[str, Any]:
        st = agent.state
        critical = [k for k, v in st.needs.items() if v <= CRITICAL_NEED_THRESHOLD]
        pressure = "high" if st.cash < st.dailyLivingCost else "normal"
        flags: Dict[str, Any] = {
            "criticalNeeds": critical,
            "financialPressure": pressure,
        }
        if st.legalStatus == LegalStatus.SUSPECTED:
            flags["pendingInvestigation"] = {"since": st.suspectedSince}
        # Surface unfilled jobs at the agent's current location as offers so
        # unemployed agents can seek work rather than idle.
        if st.employmentStatus != EmploymentStatus.EMPLOYED and st.presentLocationId:
            offers = [
                {"jobId": j.id, "occupation": j.occupation,
                 "wagePerHour": j.wagePerHour}
                for j in self.world.jobs.values()
                if j.locationId == st.presentLocationId and not j.assignedAgentId
            ]
            if offers:
                flags["employmentOffers"] = offers[:5]
        return flags

    def _current_location_block(self, agent: Agent) -> Dict[str, Any]:
        loc = self.world.locations.get(agent.state.presentLocationId or "")
        if loc is None:
            return {"id": agent.state.presentLocationId}
        return {"id": loc.id, "name": loc.name, "category": loc.category.value}

    def _build_decision_request(self, agent: Agent, sim_iso: str) -> Dict[str, Any]:
        return {
            "op": "decision", "simId": self.world.config.simId,
            "agentId": agent.id, "simTime": sim_iso,
            "persona": agent.persona.to_dict(), "state": agent.state.to_dict(),
            "currentLocation": self._current_location_block(agent),
            "reachable": self._reachable_for(agent),
            "coLocated": self._colocated_for(agent),
            "shortTermMemory": self._stm_lines(agent.id),
            "perceptionFlags": self._perception_flags(agent),
            "priceTable": self._price_table(),
        }

    def _price_table(self) -> Dict[str, Any]:
        # Compact action->cost hint sourced from location prices (best-effort).
        prices = [loc.price for loc in self.world.locations.values()
                  if loc.price is not None]
        if not prices:
            return {}
        avg = round(sum(prices) / len(prices), 2)
        return {"eat": avg, "shop": avg, "leisure": avg}

    # -- harness calls (network I/O) ---------------------------------------
    def _call_decision(self, agent: Agent, sim_iso: str) -> Optional[Dict[str, Any]]:
        if self.runtime is None:
            return None
        return self.runtime.decision(self._build_decision_request(agent, sim_iso))

    def _fan_out_decisions(self, agents: List[Agent], sim_iso: str):
        """Yield (agent, response) pairs for a batch of decision calls.

        Uses a bounded thread pool for the network-bound harness calls with
        per-agent exception isolation (a single failure never aborts the tick).
        Falls back to a sequential path when no runtime is configured or only a
        single agent is pending (keeps unit tests with fakes deterministic).
        """
        if not agents:
            return
        if self.runtime is None:
            for agent in agents:
                yield agent, None
            return
        if len(agents) == 1:
            agent = agents[0]
            try:
                yield agent, self._call_decision(agent, sim_iso)
            except Exception as e:  # noqa: BLE001
                print(f"[engine] decision error agent={agent.id}: {e}", flush=True)
                yield agent, None
            return

        requests = {a.id: self._build_decision_request(a, sim_iso) for a in agents}
        by_id = {a.id: a for a in agents}
        results: Dict[str, Optional[Dict[str, Any]]] = {}
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=DECISION_MAX_WORKERS) as ex:
            fut_to_id = {
                ex.submit(self.runtime.decision, requests[a.id]): a.id
                for a in agents
            }
            try:
                for fut in concurrent.futures.as_completed(
                        fut_to_id, timeout=DECISION_BATCH_DEADLINE_SEC):
                    aid = fut_to_id[fut]
                    try:
                        results[aid] = fut.result()
                    except Exception as e:  # noqa: BLE001
                        print(f"[engine] decision error agent={aid}: {e}", flush=True)
                        results[aid] = None
            except concurrent.futures.TimeoutError:
                for fut, aid in fut_to_id.items():
                    results.setdefault(aid, None)
        # Deterministic apply order (by agent id) regardless of completion order.
        for aid in sorted(by_id):
            yield by_id[aid], results.get(aid)

    def _apply_decision(self, agent: Agent, resp: Optional[Dict[str, Any]],
                        sim_iso: str) -> None:
        """Apply a harness decision response to the agent + log the thought.

        When ``resp`` is None (no runtime, harness error, or throttle) we do NOT
        idle forever: we run the deterministic heuristic decision engine so
        agents keep living — eating, sleeping, working, and (crucially)
        socialising toward co-located agents so conversations still form.
        """
        if resp is None:
            self._apply_heuristic_decision(agent, sim_iso)
            return
        self._record_usage(resp, sim_iso, agent.id)
        action_dict = resp.get("action")
        # When the harness yields nothing actionable, OR returns a bare "idle"
        # (its own safe-fallback when the LLM call fails / output is
        # unparseable / it genuinely stalls), we do NOT let the agent idle
        # forever. We run the deterministic heuristic instead so the world
        # stays alive — agents eat, work, travel, and socialise (which is what
        # actually forms conversations). A genuine idle only survives when the
        # heuristic itself also decides idle is the right call (e.g. detained,
        # or deep night with high energy).
        harness_type = (action_dict or {}).get("type")
        if not action_dict or harness_type == ActionType.IDLE.value:
            self._apply_heuristic_decision(agent, sim_iso)
            return
        action_dict.setdefault("startedSimTime", sim_iso)
        # Attach a straight-line route for travel actions so the SPA animates
        # movement and the agent visibly relocates (Req 8 / 15).
        try:
            action = Action.from_dict(action_dict)
        except Exception:  # noqa: BLE001 — malformed enum etc.
            action = Action.from_dict({
                "type": "idle", "targetType": "location",
                "targetId": agent.state.presentLocationId or agent.persona.homeLocationId,
                "expectedDurationMin": 10, "startedSimTime": sim_iso})
        self._attach_travel_route(agent, action)
        agent.state.currentAction = action
        # Capture a compact, human-readable "thought process": the LLM's
        # reasoning plus a snapshot of what the agent perceived (Req 14.4).
        reasoning = resp.get("reasoning") or ""
        st = agent.state
        perception = {
            "simTime": sim_iso,
            "locationId": st.presentLocationId,
            "needs": dict(st.needs) if st.needs else {},
            "cash": float(st.cash),
            "legalStatus": st.legalStatus.value,
            "employmentStatus": st.employmentStatus.value,
        }
        self.event_log.append(
            sim_iso, "action",
            f"{agent.id} -> {action.type.value}"
            + (f": {reasoning}" if reasoning else ""),
            agents=[agent.id],
            detail={
                "kind": "accepted",
                "action": action.to_dict(),
                "reasoning": reasoning,
                "perceptionInput": perception,
            })
        # Mirror the decision into short-term memory for continuity.
        self._remember(agent.id,
                       f"{sim_iso}: decided to {action.type.value}"
                       + (f" — {reasoning}" if reasoning else ""))

    def _attach_travel_route(self, agent: Agent, action: Action) -> None:
        """Attach a straight-line route + travel mode/duration to a travel action.

        No-op for non-travel or non-location targets; defensive against
        out-of-bounds destinations (leaves the action untouched on failure).
        """
        if action.type != ActionType.TRAVEL or action.targetType != TargetType.LOCATION:
            return
        dest = self.world.locations.get(action.targetId)
        if dest is None:
            return
        try:
            route = self.movement.compute_route(
                agent.state.lat, agent.state.lon, dest)
            action.route = route.coords
            action.travelMode = route.mode
            action.expectedDurationMin = route.duration_min
        except Exception:  # noqa: BLE001 — out-of-bounds etc.
            pass

    def _apply_heuristic_decision(self, agent: Agent, sim_iso: str) -> None:
        """Deterministic fallback decision when the harness returns nothing.

        Produces a sensible action from needs/time/location/co-location, builds
        it via ``Action.from_dict``, attaches a travel route, and logs an
        ``action`` event with detail kind ``heuristic``. Falls back to a genuine
        idle only if the heuristic itself fails.
        """
        try:
            action_dict = heuristic_decision(agent, self.world, sim_iso)
            action_dict.setdefault("startedSimTime", sim_iso)
            action = Action.from_dict(action_dict)
        except Exception as e:  # noqa: BLE001 — never abort a tick
            print(f"[engine] heuristic error agent={agent.id}: {e}", flush=True)
            agent.state.currentAction = Action.from_dict({
                "type": "idle", "targetType": "location",
                "targetId": agent.state.presentLocationId or agent.persona.homeLocationId,
                "expectedDurationMin": 10, "startedSimTime": sim_iso})
            self.event_log.append(sim_iso, "action", f"idle fallback for {agent.id}",
                                  agents=[agent.id], detail={"kind": "fallback"})
            return
        self._attach_travel_route(agent, action)
        agent.state.currentAction = action
        st = agent.state
        perception = {
            "simTime": sim_iso,
            "locationId": st.presentLocationId,
            "needs": dict(st.needs) if st.needs else {},
            "cash": float(st.cash),
            "legalStatus": st.legalStatus.value,
            "employmentStatus": st.employmentStatus.value,
        }
        self.event_log.append(
            sim_iso, "action",
            f"{agent.id} -> {action.type.value} (heuristic)",
            agents=[agent.id],
            detail={
                "kind": "heuristic",
                "action": action.to_dict(),
                "reasoning": "heuristic decision (no LLM runtime)",
                "perceptionInput": perception,
            })
        self._remember(agent.id,
                       f"{sim_iso}: decided to {action.type.value} (heuristic)")

    # -- planning & reflection (thought logging) ---------------------------
    def _should_plan(self, agent: Agent, sim_iso: str, sim_date: str) -> bool:
        if self.runtime is None:
            return False
        if self._planned_on.get(agent.id) == sim_date:
            return False
        # Plan once around the agent's wake time (or first tick we see them).
        wake = (agent.persona.wakeTime or "07:00")[:5]
        return sim_iso[11:16] >= wake

    def _trigger_plan(self, agent: Agent, sim_iso: str) -> None:
        if self.runtime is None:
            return
        request = {
            "op": "plan", "simId": self.world.config.simId,
            "agentId": agent.id, "simTime": sim_iso,
            "persona": agent.persona.to_dict(), "state": agent.state.to_dict(),
            "reachable": self._reachable_for(agent),
            "longTermMemory": self._stm_lines(agent.id),
        }
        try:
            resp = self.runtime.plan(request)
        except Exception as e:  # noqa: BLE001
            print(f"[engine] plan error agent={agent.id}: {e}", flush=True)
            return
        if not isinstance(resp, dict):
            return
        self._record_usage(resp, sim_iso, agent.id)
        plan = resp.get("plan")
        if not isinstance(plan, list) or not plan:
            return
        agent.state.dayPlan = plan
        summary = " -> ".join(p.get("type", "?") for p in plan[:12])
        self.event_log.append(
            sim_iso, "planning",
            f"{agent.id} planned the day: {summary}",
            agents=[agent.id],
            detail={"kind": "day-plan", "plan": plan,
                    "reasoning": resp.get("reasoning", "")})
        self._remember(agent.id, f"{sim_iso}: planned day: {summary}")

    def _trigger_reflect(self, agent: Agent, sim_iso: str) -> None:
        if self.runtime is None:
            return
        request = {
            "op": "reflect", "simId": self.world.config.simId,
            "agentId": agent.id, "simTime": sim_iso,
            "persona": agent.persona.to_dict(),
            "shortTermMemory": self._stm_lines(agent.id),
            "longTermMemory": self._stm_lines(agent.id),
        }
        try:
            resp = self.runtime.reflect(request)
        except Exception as e:  # noqa: BLE001
            print(f"[engine] reflect error agent={agent.id}: {e}", flush=True)
            return
        if not isinstance(resp, dict):
            return
        self._record_usage(resp, sim_iso, agent.id)
        reflections = resp.get("reflections")
        if not isinstance(reflections, list) or not reflections:
            return
        for r in reflections:
            text = (r or {}).get("text") if isinstance(r, dict) else None
            if not text:
                continue
            self.event_log.append(
                sim_iso, "memory",
                f"{agent.id} reflected: {text}",
                agents=[agent.id],
                detail={"kind": "reflection", "text": text,
                        "sourceMemoryIds": (r or {}).get("sourceMemoryIds", [])})
            self._remember(agent.id, f"{sim_iso}: reflection: {text}")

    # -- budget + short-term memory helpers --------------------------------
    def _record_usage(self, resp: Dict[str, Any], sim_iso: str,
                      agent_id: str) -> None:
        """Record token usage + spend from a harness response (Req 18.3)."""
        usage = resp.get("tokenUsage") if isinstance(resp, dict) else None
        if not isinstance(usage, dict):
            return
        model_id = usage.get("modelId", "")
        purpose = usage.get("purpose", "decision_cycle")
        try:
            in_tok = int(usage.get("inputTokens", 0) or 0)
            out_tok = int(usage.get("outputTokens", 0) or 0)
        except (TypeError, ValueError):
            in_tok = out_tok = 0
        cost = self.budget.record_invocation(model_id, purpose, in_tok, out_tok)
        self.event_log.append(
            sim_iso, "model",
            f"invocation {purpose} for {agent_id} ({in_tok}+{out_tok} tok)",
            agents=[agent_id],
            detail={"kind": "invocation", "modelId": model_id,
                    "purpose": purpose, "inputTokens": in_tok,
                    "outputTokens": out_tok, "costUSD": cost})

    def _remember(self, agent_id: str, line: str) -> None:
        buf = self._stm.setdefault(agent_id, [])
        buf.append(line)
        if len(buf) > STM_LIMIT:
            del buf[: len(buf) - STM_LIMIT]

    def _stm_lines(self, agent_id: str) -> List[Dict[str, Any]]:
        return [{"text": t} for t in self._stm.get(agent_id, [])]

    def _run_conversations(self, sim_iso: str) -> int:
        """Form, run, and resolve conversations among co-located socialisers.

        For each not-yet-conversing agent whose current action is
        socialise -> <agent>, attempt to form a conversation with co-located
        participants, drive utterances via the harness, resolve relationship
        effects, and append a `conversation` event carrying the full transcript
        (participants + utterances) so the SPA can render it. Returns the number
        of conversations that produced a persisted transcript.

        Budget/availability: only runs when a runtime is present and the budget
        allows a decision-class invocation; utterance failures truncate the
        conversation gracefully (Req 10.8). When no runtime is configured, a
        deterministic local utterance fallback keeps conversations flowing so
        the world stays alive without an LLM.
        """
        use_local = self.runtime is None

        # Index agents by their current location for co-location checks.
        by_location: Dict[str, List[Agent]] = {}
        for agent in self.world.agents.values():
            loc = agent.state.presentLocationId
            if loc:
                by_location.setdefault(loc, []).append(agent)

        in_conversation: set[str] = set()
        started = 0
        convo_counter = 0

        for agent in self.world.agents.values():
            act = agent.state.currentAction
            if act is None or act.type != ActionType.SOCIALISE:
                continue
            if agent.id in in_conversation:
                continue
            loc = agent.state.presentLocationId
            if not loc:
                continue
            colocated = [a for a in by_location.get(loc, []) if a.id != agent.id]
            if not colocated:
                continue
            if not use_local and not self.budget.can_start_decision():
                break

            convo_counter += 1
            convo_id = f"convo-{sim_iso}-{agent.id}-{convo_counter}"
            convo, _declined = self.social.match_conversation(
                initiator=agent, colocated=colocated,
                in_conversation=in_conversation, conversation_id=convo_id,
                location_id=loc)
            if convo is None:
                continue

            # Mark participants as busy so they aren't double-matched this tick.
            for pid in convo.participants:
                in_conversation.add(pid)

            def utterance_provider(conversation, speaker_id, _self=self,
                                   _sim=sim_iso, _local=use_local):
                # Local, LLM-free fallback: cheap deterministic small talk that
                # can reference remembered injected events. Keeps conversations
                # forming (>=2 utterances) when the harness is unavailable.
                if _local:
                    persona = _self.world.agents.get(speaker_id)
                    if persona is None:
                        return None
                    loc_obj = _self.world.locations.get(
                        persona.state.presentLocationId or "")
                    loc_name = loc_obj.name if loc_obj is not None else "here"
                    turn_index = len(conversation.utterances)
                    mem = [t for t in _self._stm.get(speaker_id, [])]
                    return local_utterance(persona.persona, loc_name,
                                           turn_index, _sim, memory_lines=mem)
                payload = {
                    "op": "utterance",
                    "simId": _self.world.config.simId,
                    "agentId": speaker_id,
                    "simTime": _sim,
                    "conversation": {
                        "id": conversation.id,
                        "participants": conversation.participants,
                        "locationId": conversation.location_id,
                        "utterancesSoFar": [
                            {"speaker": u.speaker, "text": u.text}
                            for u in conversation.utterances
                        ],
                    },
                }
                persona = _self.world.agents.get(speaker_id)
                if persona is not None:
                    payload["persona"] = persona.persona.to_dict()
                payload["longTermMemory"] = _self._stm_lines(speaker_id)
                resp = _self.runtime.utterance(payload)
                if not isinstance(resp, dict):
                    return None
                _self._record_usage(resp, _sim, speaker_id)
                text = resp.get("utterance")
                return text if isinstance(text, str) and text.strip() else None

            # Ensure the local fallback produces at least MIN_UTTERANCES so the
            # conversation is persisted (the harness path self-limits via None).
            max_u = 4 if use_local else 10
            try:
                self.social.run_conversation(convo, utterance_provider,
                                             max_utterances=max_u)
            except Exception:
                convo.truncated = True

            outcome = self.social.resolve(convo)

            # Only persist conversations that actually exchanged utterances.
            if len(convo.utterances) >= 1:
                self.event_log.append(
                    sim_iso, "conversation",
                    f"conversation-ended at {loc} ({len(convo.utterances)} lines)",
                    agents=list(convo.participants),
                    location_id=loc,
                    detail={
                        "kind": "conversation-ended",
                        "conversationId": convo.id,
                        "participants": list(convo.participants),
                        "locationId": loc,
                        "utterances": [
                            {"speaker": u.speaker, "text": u.text}
                            for u in convo.utterances
                        ],
                        "truncated": convo.truncated,
                        "utteranceCount": outcome.utterance_count,
                    })
                started += 1
                # Mirror a compact conversation summary into each participant's
                # short-term memory for behavioural continuity.
                snippet = "; ".join(
                    f"{u.speaker}: {u.text}" for u in convo.utterances[:4])
                for pid in convo.participants:
                    self._remember(pid, f"{sim_iso}: talked at {loc} — {snippet}")

        return started

    # -- injected world events (Task 2) ------------------------------------
    def _process_injected_events(self, sim_iso: str) -> int:
        """Propagate any NEW injected events once. Never aborts a tick.

        For each unprocessed event in ``world.injectedEvents`` whose simTime has
        arrived, run the propagation engine, push a memory line into each aware
        agent's short-term memory, set avoidance/attraction hints that bias the
        heuristic, and emit a ``system`` event summarising how many agents became
        aware. Returns the number of events processed this tick.
        """
        events = getattr(self.world, "injectedEvents", None)
        if not events:
            return 0
        processed = 0
        for event_id in sorted(events.keys()):
            if event_id in self.world.processedEventIds:
                continue
            event = events[event_id]
            # Only fire once the event's sim time has been reached.
            try:
                if event.simTime and event.simTime > sim_iso:
                    continue
            except TypeError:
                pass
            try:
                self._propagate_one_event(event, sim_iso)
            except Exception as e:  # noqa: BLE001 — never abort a tick
                print(f"[engine] injected-event error id={event_id}: {e}",
                      flush=True)
            finally:
                self.world.processedEventIds.add(event_id)
                processed += 1
        return processed

    def _propagate_one_event(self, event: "Injected_Event", sim_iso: str) -> None:
        result = self.propagation.propagate(event, self.world)
        avoid_expiry = add_sim_minutes(sim_iso, AVOID_TTL_MIN)
        attract_expiry = add_sim_minutes(sim_iso, ATTRACT_TTL_MIN)
        for aid in result.aware_agent_ids:
            agent = self.world.agents.get(aid)
            if agent is None:
                continue
            # Push the event into short-term memory so decisions/utterances can
            # reference it (agents "talk about" the explosion/festival).
            self._remember(aid, result.memory_line)
            # Scary events near a known location -> avoid it for a while.
            if result.scary and result.avoided_location_id:
                agent.state.avoidedLocations[result.avoided_location_id] = avoid_expiry
            # Positive events -> some agents become attracted to the location.
            if result.attractor_location_id and not result.scary:
                agent.state.attractorLocation = {
                    "locationId": result.attractor_location_id,
                    "expiry": attract_expiry,
                }
        self.event_log.append(
            sim_iso, "system",
            f"injected event '{event.title or event.id}' "
            f"({event.scale}/{event.severity}) — {result.aware_count} agents aware",
            location_id=event.locationId,
            detail={
                "kind": "injected-event",
                "eventId": event.id,
                "title": event.title,
                "scale": event.scale,
                "severity": event.severity,
                "awareCount": result.aware_count,
                "awareAgentIds": sorted(result.aware_agent_ids),
                "scary": result.scary,
                "positive": result.positive,
                "avoidedLocationId": result.avoided_location_id,
                "attractorLocationId": result.attractor_location_id,
            })


__all__ = ["Ticker", "WorldState", "TickReport", "AgentRuntimeClient"]
