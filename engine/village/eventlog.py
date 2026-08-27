"""Event_Log — append-only observability log (Requirement 14).

Monotonic zero-padded 20-digit sequence, category/agent GSI query support,
filtered retrieval capped at 500 with a `more` flag, ascending simTime then
realTime ordering, decision-trail retrieval, and summary aggregation (14.9).

Storage is injectable: an in-memory store is provided for tests; the ticker
uses the DynamoStore-backed writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol

from .models import EventLogEntry

SEQ_WIDTH = 20
MAX_QUERY = 500
LEGAL_STATUSES = ("clear", "suspected", "charged", "detained")
EMPLOYMENT_STATUSES = ("employed", "unemployed", "suspended")


class EventLogModifyError(Exception):
    """Raised when a modify/remove is attempted (append-only, Req 14.7)."""


def seq20(seq: int) -> str:
    """Zero-padded 20-digit sequence string for lexicographic ordering."""
    return str(seq).zfill(SEQ_WIDTH)


@dataclass
class QueryResult:
    entries: List[EventLogEntry]
    more: bool


class EventSink(Protocol):
    """Persistence sink for appended entries."""
    def append(self, entry: EventLogEntry) -> None: ...
    def all(self) -> List[EventLogEntry]: ...


class InMemoryEventSink:
    def __init__(self):
        self._entries: List[EventLogEntry] = []

    def append(self, entry: EventLogEntry) -> None:
        self._entries.append(entry)

    def all(self) -> List[EventLogEntry]:
        return list(self._entries)


class Event_Log:
    """Append-only event log with monotonic sequence and filtered queries."""

    def __init__(self, sink: Optional[EventSink] = None,
                 real_clock: Optional[Callable[[], datetime]] = None,
                 start_seq: int = 0):
        self._sink = sink if sink is not None else InMemoryEventSink()
        self._seq = start_seq
        self._real_clock = real_clock or (lambda: datetime.now().astimezone())

    # -- append (Req 14.1) --------------------------------------------------
    def append(self, sim_time: str, category: str, description: str,
               agents: Optional[List[str]] = None,
               location_id: Optional[str] = None,
               detail: Optional[Dict[str, Any]] = None,
               real_time: Optional[str] = None) -> EventLogEntry:
        self._seq += 1
        rt = real_time or self._real_clock().replace(microsecond=0).isoformat()
        # description constrained to 1..500 chars.
        desc = (description or "")[:500] or "-"
        entry = EventLogEntry(
            seq=self._seq, simTime=sim_time, realTime=rt, category=category,
            agents=list(agents or []), locationId=location_id,
            description=desc, detail=detail,
        )
        self._sink.append(entry)
        return entry

    # append-only guard (Req 14.7)
    def modify(self, *_args, **_kwargs):
        raise EventLogModifyError("Event_Log accepts appends only")

    def remove(self, *_args, **_kwargs):
        raise EventLogModifyError("Event_Log accepts appends only")

    # -- ordering helper (Req 14.3) ----------------------------------------
    @staticmethod
    def _sort_key(e: EventLogEntry):
        return (e.simTime, e.realTime, e.seq)

    def _ordered(self) -> List[EventLogEntry]:
        return sorted(self._sink.all(), key=self._sort_key)

    # -- query (Req 14.2) ---------------------------------------------------
    def query(self, category: Optional[str] = None,
              agent_id: Optional[str] = None,
              from_sim_time: Optional[str] = None,
              to_sim_time: Optional[str] = None,
              cursor: int = 0,
              limit: int = MAX_QUERY) -> QueryResult:
        limit = min(MAX_QUERY, max(1, limit))
        matches: List[EventLogEntry] = []
        for e in self._ordered():
            if category is not None and e.category != category:
                continue
            if agent_id is not None and agent_id not in e.agents:
                continue
            if from_sim_time is not None and e.simTime < from_sim_time:
                continue
            if to_sim_time is not None and e.simTime > to_sim_time:
                continue
            matches.append(e)
        window = matches[cursor:cursor + limit]
        more = len(matches) > cursor + limit
        return QueryResult(entries=window, more=more)

    # -- decision trail (Req 14.4 / 14.5) ----------------------------------
    def decision_trail(self, action_event_seq: int) -> Optional[Dict[str, Any]]:
        """Return the decision-trail detail for an action event, or None."""
        for e in self._sink.all():
            if e.seq == action_event_seq and e.category == "action":
                detail = e.detail or {}
                return {
                    "perceptionInput": detail.get("perceptionInput"),
                    "retrievedMemoryIds": detail.get("retrievedMemoryIds", []),
                    "action": detail.get("action"),
                }
        return None

    # -- summary (Req 14.9) -------------------------------------------------
    def summary(self, sim_time: str, day_start_sim_time: str,
                legal_counts: Dict[str, int],
                employment_counts: Dict[str, int]) -> Dict[str, Any]:
        """Aggregate a simulation summary for the current day up to sim_time."""
        crime_count = 0
        conversation_count = 0
        for e in self._ordered():
            if e.simTime < day_start_sim_time or e.simTime > sim_time:
                continue
            if e.category == "crime":
                crime_count += 1
            elif e.category == "conversation":
                # count conversation-ended entries only
                if (e.detail or {}).get("kind") == "conversation-ended" \
                        or "conversation-ended" in (e.description or ""):
                    conversation_count += 1
        return {
            "simTime": sim_time,
            "legalStatusCounts": {s: int(legal_counts.get(s, 0)) for s in LEGAL_STATUSES},
            "employmentStatusCounts": {s: int(employment_counts.get(s, 0)) for s in EMPLOYMENT_STATUSES},
            "crimeCount": crime_count,
            "conversationCount": conversation_count,
        }

    @property
    def current_seq(self) -> int:
        return self._seq


__all__ = [
    "Event_Log", "QueryResult", "EventSink", "InMemoryEventSink",
    "EventLogModifyError", "seq20", "MAX_QUERY",
]
