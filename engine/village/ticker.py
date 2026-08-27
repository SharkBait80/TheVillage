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
from .heuristics import heuristic_decision, local_utterance, in_world_reason
from .law import Law_Enforcement_Engine
from .crime import CrimeValidationError
from .models import (Action, ActionType, Agent, Config, CrimeType,
                     EmploymentStatus, Job, LegalStatus, Location,
                     LocationCategory, SimStatus, TargetType)
from .movement import Movement_Engine, haversine_m
from .needs import (CONVO_MIN_MINUTES, apply_decay_tick,
                    apply_energy_recovery_tick, on_conversation_complete,
                    on_eat_complete, on_leisure_complete, update_critical_flags)
from .social import Social_Engine

# Bounded concurrency for per-agent harness calls within a tick. Kept small to
# respect the invocation budget and the AgentCore Runtime; network I/O bound so
# a modest pool keeps 25 agents responsive without saturating the engine task.
DECISION_MAX_WORKERS = 8
# Best-effort ceiling on how long we wait for the whole decision fan-out in a
# single tick before proceeding (real per-call timeouts live in the boto3
# client config on the engine side). Kept short so a slow harness cannot stall
# the world clock — undecided agents fall back to the local heuristic.
DECISION_BATCH_DEADLINE_SEC = 8.0
# Hard cap on how many agents call the network-bound harness in a single tick.
# At large populations (hundreds of agents) an unbounded fan-out cannot finish
# within the tick budget and would freeze the clock; the remainder are decided
# instantly by the deterministic heuristic engine.
DECISION_MAX_PER_TICK = 8
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
        # Planning calls the harness synchronously; cap how many run per tick so
        # a wake-time surge across hundreds of agents cannot stall the clock.
        # Un-planned agents simply plan on a later tick (or rely on heuristics).
        sim_date = sim_iso[:10]
        planned_this_tick = 0
        for agent in self.world.agents.values():
            if self._should_plan(agent, sim_iso, sim_date):
                if planned_this_tick < DECISION_MAX_PER_TICK and self.budget.can_start_decision():
                    self._trigger_plan(agent, sim_iso)
                    planned_this_tick += 1
                    self._planned_on[agent.id] = sim_date
                # else: leave unmarked so it gets a chance on a subsequent tick.

        # 3b. fan out decision cycles for idle agents with bounded concurrency.
        # Building perception + calling the harness is network-I/O bound, so we
        # parallelise the calls (<=8 in flight) and then apply results on the
        # main thread to keep the rest of the tick deterministic.
        idle_agents = [a for a in self.world.agents.values()
                       if a.state.currentAction is None]
        pending: List[Agent] = []
        overflow: List[Agent] = []
        for agent in idle_agents:
            # Cap how many agents call the (network-bound) harness per tick. At
            # large populations, fanning out hundreds of harness calls per tick
            # cannot complete within the tick budget and would stall the clock;
            # the overflow is decided instantly by the deterministic heuristic
            # engine so the world stays live and conversations still form.
            if len(pending) >= DECISION_MAX_PER_TICK:
                overflow.append(agent)
                continue
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
        # Overflow agents: instant local heuristic decision (no harness call).
        for agent in overflow:
            self._apply_heuristic_decision(agent, sim_iso)
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
            # Bounded per tick: reflection is a synchronous harness call, so an
            # all-agent burst at the day boundary would otherwise stall the clock.
            reflected_this_tick = 0
            for agent in self.world.agents.values():
                if reflected_this_tick < DECISION_MAX_PER_TICK and self.budget.can_start_decision():
                    self._trigger_reflect(agent, sim_iso)
                    reflected_this_tick += 1
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
                self._on_action_complete(agent, act, tick)
                agent.state.currentAction = None
        else:
            act.progress = min(1.0, act.progress + 1.0 / max(1, act.expectedDurationMin))
            if act.progress >= 1.0:
                self._on_action_complete(agent, act, tick)
                agent.state.currentAction = None

    # -- action completion side-effects ------------------------------------
    def _on_action_complete(self, agent: Agent, action: Action, tick: Tick) -> None:
        """Apply the real economic / need / crime effects of a finished action.

        Runs once, at the moment an action reaches progress>=1.0, BEFORE the
        action is cleared. Every branch is defensive: an error resolving one
        agent's action must never abort the tick for the rest of the world.

        Wired behaviour (DESIGN §8 / Req 5, 9, 11, 12):
          - work      -> credit wages for shift overlap; register attendance
          - eat       -> +hunger recovery (food/home, >=15 min)
          - leisure   -> +fun recovery (>=30 min)
          - shop      -> debit purchase cost at the retail/food location
          - commit_crime -> resolve crime, apply cash transfer + law enforcement
        Travel/socialise/idle/sleep have no completion economics here (sleep
        energy recovery is applied per-tick; social recovery is credited when a
        conversation resolves in ``_run_conversations``).
        """
        try:
            atype = action.type
            if atype == ActionType.WORK:
                self._complete_work(agent, action, tick)
            elif atype == ActionType.EAT:
                self._complete_eat(agent, action)
            elif atype == ActionType.LEISURE:
                self._complete_leisure(agent, action)
            elif atype == ActionType.SHOP:
                self._complete_shop(agent, action)
            elif atype == ActionType.COMMIT_CRIME:
                self._complete_crime(agent, action, tick)
        except Exception as e:  # noqa: BLE001 — never abort a tick
            print(f"[engine] action-complete error agent={agent.id} "
                  f"type={getattr(action, 'type', '?')}: {e}", flush=True)

    def _action_sim_iso(self, tick: Tick) -> str:
        return tick.sim_time.replace(microsecond=0).isoformat()

    def _job_for(self, agent: Agent) -> Optional[Job]:
        jid = agent.state.jobId
        if not jid:
            return None
        return self.world.jobs.get(jid)

    def _take_open_job_here(self, agent: Agent, sim_iso: str) -> Optional[Job]:
        """Let an unemployed agent take an unassigned job at their present
        location by working there (Req 9.9). Returns the taken Job or None."""
        if agent.state.employmentStatus == EmploymentStatus.EMPLOYED:
            return None
        loc_id = agent.state.presentLocationId
        if not loc_id:
            return None
        for job in self.world.jobs.values():
            if job.locationId != loc_id or job.assignedAgentId:
                continue
            if self.economy.take_job(agent, job):
                self.event_log.append(
                    sim_iso, "employment",
                    f"{agent.id} took a job as {job.occupation}",
                    agents=[agent.id],
                    detail={"kind": "job-taken", "jobId": job.id,
                            "occupation": job.occupation,
                            "wagePerHour": job.wagePerHour})
                self._remember(agent.id,
                               f"{sim_iso}: started a new job as {job.occupation}")
                return job
        return None

    def _complete_work(self, agent: Agent, action: Action, tick: Tick) -> None:
        """Credit wages for the portion of a work action inside the shift, and
        register shift attendance (missed-shift streak / auto-unemployment)."""
        sim_iso = self._action_sim_iso(tick)
        job = self._job_for(agent)
        if job is None:
            # Unemployed agent working at a location with an open job takes it
            # (Req 9.9), then earns from this same shift.
            job = self._take_open_job_here(agent, sim_iso)
            if job is None:
                return
        work_location_id = action.targetId or agent.state.presentLocationId or ""
        # Determine when the action started so we can compute shift overlap.
        started = action.startedSimTime or sim_iso
        try:
            start_dt = datetime.fromisoformat(started)
        except (TypeError, ValueError):
            start_dt = tick.sim_time
        overlap = self.economy.shift_overlap_minutes(
            job, start_dt, action.expectedDurationMin)
        # Attendance: present at the workplace counts as attending the shift.
        workplace = self.world.locations.get(job.locationId)
        attended = False
        if workplace is not None:
            attended = self.economy.attended_shift(agent.state, job, workplace)
        became_unemployed = self.economy.register_shift_attendance(
            agent, job, attended)
        if became_unemployed:
            self.event_log.append(
                sim_iso, "employment",
                f"{agent.id} lost their job after repeated missed shifts",
                agents=[agent.id],
                detail={"kind": "job-lost", "jobId": job.id})
            return
        wage = self.economy.credit_wage(
            agent.state, job, work_location_id,
            action.expectedDurationMin, overlap)
        if wage.earned and wage.credited > 0:
            self.event_log.append(
                sim_iso, "employment",
                f"{agent.id} earned {wage.credited:.2f} for a work shift",
                agents=[agent.id],
                detail={"kind": "wage-credited", "amount": wage.credited,
                        "balance": wage.new_balance, "jobId": job.id,
                        "shiftMinutes": overlap})
            self._remember(agent.id,
                           f"{sim_iso}: earned {wage.credited:.2f} at work")

    def _complete_eat(self, agent: Agent, action: Action) -> None:
        loc = self.world.locations.get(action.targetId
                                       or agent.state.presentLocationId or "")
        category = loc.category if loc is not None else LocationCategory.FOOD
        is_home = (loc is not None
                   and loc.id == agent.persona.homeLocationId)
        on_eat_complete(agent.state, action.expectedDurationMin, category, is_home)

    def _complete_leisure(self, agent: Agent, action: Action) -> None:
        on_leisure_complete(agent.state, action.expectedDurationMin)

    def _complete_shop(self, agent: Agent, action: Action) -> None:
        """Debit the purchase cost of a completed shop/eat-out action."""
        loc = self.world.locations.get(action.targetId
                                       or agent.state.presentLocationId or "")
        if loc is None:
            return
        result = self.economy.purchase(agent.state, loc)
        sim_iso = action.startedSimTime or ""
        if result.accepted and result.debited > 0:
            self.event_log.append(
                sim_iso or (agent.persistedSimTime or ""),
                "economy",
                f"{agent.id} spent {result.debited:.2f} at {loc.name}",
                agents=[agent.id], location_id=loc.id,
                detail={"kind": "purchase", "amount": result.debited,
                        "balance": result.new_balance, "locationId": loc.id})

    def _complete_crime(self, agent: Agent, action: Action, tick: Tick) -> None:
        """Validate, resolve, and law-enforce a completed commit_crime action
        (Req 11 / 12). Cash transfer, witnesses, detection, and the
        suspected/charged/detained progression all run here."""
        sim_iso = self._action_sim_iso(tick)
        crime_type = action.crimeType
        if crime_type is None:
            return
        target_type = action.targetType
        target_id = action.targetId
        # Resolve target position + optional target agent.
        target_agent: Optional[Agent] = None
        if target_type == TargetType.AGENT:
            target_agent = self.world.agents.get(target_id)
            if target_agent is None:
                return
            target_pos = (target_agent.state.lat, target_agent.state.lon)
        else:
            loc = self.world.locations.get(target_id)
            if loc is None:
                return
            target_pos = (loc.lat, loc.lon)
        try:
            self.crime.validate(agent, crime_type, target_type, target_id,
                                target_pos)
        except CrimeValidationError as e:
            self.event_log.append(
                sim_iso, "crime",
                f"{agent.id} crime attempt invalid ({e.check})",
                agents=[agent.id],
                detail={"kind": "crime-invalid", "check": e.check})
            return
        others = list(self.world.agents.values())
        witnesses = self.crime.find_witnesses(agent, others, target_pos)
        # Stolen amount: theft/burglary steal an amount bounded by the engine.
        stolen = 0
        if crime_type in (CrimeType.THEFT, CrimeType.BURGLARY):
            if target_agent is not None:
                stolen = int(max(1.0, min(500.0, target_agent.state.cash)))
            else:
                stolen = 100
        resolution = self.crime.resolve(
            agent, crime_type, target_type, target_id, tick.sim_time,
            witnesses, stolen_amount=stolen, target_agent=target_agent)
        crime_event = resolution.crime_event
        self.event_log.append(
            sim_iso, "crime",
            f"{agent.id} attempted {crime_type.value} -> {crime_event.outcome.value}",
            agents=[agent.id] + list(witnesses),
            location_id=(target_id if target_type == TargetType.LOCATION else None),
            detail={"kind": "crime-resolved",
                    "crimeType": crime_type.value,
                    "outcome": crime_event.outcome.value,
                    "witnesses": list(witnesses),
                    "stolenAmount": crime_event.stolenAmount,
                    "cashDelta": resolution.cash_delta})
        # Push crime memory to perpetrator, target, and witnesses (Req 11.4).
        for rid in resolution.memory_recipients:
            self._remember(rid,
                           f"{sim_iso}: {agent.id} {crime_event.outcome.value} "
                           f"a {crime_type.value}")
        # Law enforcement: detection + suspected/charged/detained (Req 12).
        if self.law is not None:
            law_outcome = self.law.process_crime(agent, crime_event, tick.sim_time)
            for desc in law_outcome.events:
                self.event_log.append(
                    sim_iso, "legal", desc, agents=[agent.id],
                    detail={"kind": "law", "detection": law_outcome.detection.value,
                            "status": agent.state.legalStatus.value})

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
        # NOTE: we deliberately do NOT use a `with ThreadPoolExecutor(...)` block.
        # Its __exit__ calls shutdown(wait=True), which blocks until EVERY
        # submitted harness call returns — so a slow/unreachable AgentCore
        # Runtime (each call can take the full boto3 read-timeout) would wedge
        # the entire engine tick loop for minutes. Instead we honour the batch
        # deadline and then shut the pool down WITHOUT waiting, cancelling any
        # queued futures so the tick proceeds with heuristic fallbacks for
        # agents whose decision didn't complete in time.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=DECISION_MAX_WORKERS)
        try:
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
        finally:
            # Non-blocking: cancel queued futures; abandon in-flight ones (their
            # daemon threads exit when the call finally returns/times out).
            ex.shutdown(wait=False, cancel_futures=True)
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
            self.event_log.append(sim_iso, "action",
                                  f"{agent.id} -> idle: {in_world_reason('idle', agent.id, sim_iso)}",
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
        # In-world, player-facing justification. `detail.kind` still records
        # "heuristic" for internal analytics, but nothing user-visible
        # (description / reasoning / memory) references the engine or the LLM.
        reasoning = in_world_reason(action.type.value, agent.id, sim_iso)
        self.event_log.append(
            sim_iso, "action",
            f"{agent.id} -> {action.type.value}: {reasoning}",
            agents=[agent.id],
            detail={
                "kind": "heuristic",
                "action": action.to_dict(),
                "reasoning": reasoning,
                "perceptionInput": perception,
            })
        self._remember(agent.id,
                       f"{sim_iso}: decided to {action.type.value} — {reasoning}")

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

        # Clear last tick's live-conversation markers; we re-stamp the ones that
        # form this tick so the SPA's /state renders current conversation
        # indicators (it derives them from each agent's state.conversation).
        for agent in self.world.agents.values():
            if getattr(agent.state, "conversation", None):
                agent.state.conversation = None

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
                    # Address the conversation partner by name (never "agent").
                    partner_name = None
                    for pid in conversation.participants:
                        if pid == speaker_id:
                            continue
                        other = _self.world.agents.get(pid)
                        if other is not None:
                            partner_name = other.persona.name
                            break
                    return local_utterance(persona.persona, loc_name,
                                           turn_index, _sim, memory_lines=mem,
                                           partner_name=partner_name)
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
                # Stamp a live conversation marker on each participant so the
                # SPA's /state surfaces a conversation indicator for this tick.
                for pid in convo.participants:
                    p = self.world.agents.get(pid)
                    if p is not None:
                        p.state.conversation = {
                            "participants": list(convo.participants),
                            "locationId": loc,
                        }
                # Credit social-need recovery once per conversation for each
                # participant (Req 5.4). Idempotent via creditedConversations.
                # A conversation only forms off a socialise action, so use that
                # action's planned duration as the conversation length (minutes);
                # fall back to the per-utterance minimum when unknown.
                for pid in convo.participants:
                    participant = self.world.agents.get(pid)
                    if participant is None:
                        continue
                    pact = participant.state.currentAction
                    if (pact is not None
                            and pact.type == ActionType.SOCIALISE
                            and pact.expectedDurationMin > 0):
                        convo_minutes = pact.expectedDurationMin
                    else:
                        convo_minutes = max(len(convo.utterances),
                                            outcome.utterance_count, CONVO_MIN_MINUTES)
                    on_conversation_complete(
                        participant.state, convo.id, convo_minutes)
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
