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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol

from .budget import Budget_Accountant
from .clock import DayRollover, Simulation_Clock, Tick
from .controller import Simulation_Controller
from .crime import Crime_Engine
from .economy import Economy_Engine
from .eventlog import Event_Log
from .law import Law_Enforcement_Engine
from .models import (Action, ActionType, Agent, Config, Job, LegalStatus,
                     Location, SimStatus)
from .movement import Movement_Engine
from .needs import (apply_decay_tick, apply_energy_recovery_tick,
                    update_critical_flags)
from .social import Social_Engine


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

        for agent in self.world.agents.values():
            if agent.state.currentAction is None:
                if self.budget.can_start_decision():
                    self._trigger_decision(agent, sim_iso)
                    decisions += 1
                else:
                    throttled += 1
                    self.event_log.append(
                        sim_iso, "model",
                        f"decision throttled for {agent.id}",
                        agents=[agent.id],
                        detail={"kind": "throttled"})

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
        request = {
            "op": "decision", "simId": self.world.config.simId,
            "agentId": agent.id, "simTime": sim_iso,
            "persona": agent.persona.to_dict(), "state": agent.state.to_dict(),
        }
        try:
            resp = self.runtime.decision(request)
        except Exception:
            # fallback idle (Req 6.7 / 6.8)
            agent.state.currentAction = Action.from_dict({
                "type": "idle", "targetType": "location",
                "targetId": agent.state.presentLocationId or agent.persona.homeLocationId,
                "expectedDurationMin": 10, "startedSimTime": sim_iso})
            self.event_log.append(sim_iso, "action", f"idle fallback for {agent.id}",
                                  agents=[agent.id], detail={"kind": "fallback"})
            return
        action_dict = resp.get("action")
        if action_dict:
            action_dict.setdefault("startedSimTime", sim_iso)
            agent.state.currentAction = Action.from_dict(action_dict)
            self.event_log.append(
                sim_iso, "action",
                f"{agent.id} -> {action_dict.get('type')}",
                agents=[agent.id],
                detail={"kind": "accepted", "action": action_dict})


__all__ = ["Ticker", "WorldState", "TickReport", "AgentRuntimeClient"]
