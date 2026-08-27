"""Social_Engine — conversations & relationships (Requirement 10).

The engine orchestrates and validates conversations; utterance *text* is
supplied by the harness (LLM). Relationship familiarity/sentiment adjustments
are computed deterministically from the exchanged utterances and clamped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .models import Action, ActionType, Agent, Relationship, TargetType

MAX_PARTICIPANTS = 6
MIN_UTTERANCES = 2
MAX_UTTERANCES = 10
MAX_UTTERANCE_CHARS = 500
FAMILIARITY_MILESTONE = 50   # Req 10.7
REL_CLAMP_MIN = -100
REL_CLAMP_MAX = 100
FAMILIARITY_STEP_MIN = 1
FAMILIARITY_STEP_MAX = 10
SENTIMENT_STEP_MIN = -20
SENTIMENT_STEP_MAX = 20


@dataclass
class Utterance:
    speaker: str
    text: str


@dataclass
class Conversation:
    id: str
    participants: List[str]
    location_id: str
    utterances: List[Utterance] = field(default_factory=list)
    ended: bool = False
    truncated: bool = False


@dataclass
class ConversationOutcome:
    conversation: Conversation
    memory_written: bool
    relationship_changes: List[Tuple[str, str, int, int]]  # (from,to,dfam,dsent)
    milestones: List[Tuple[str, str]]                        # (from,to)
    utterance_count: int


def _clamp_rel(v: int) -> int:
    return max(REL_CLAMP_MIN, min(REL_CLAMP_MAX, v))


class Social_Engine:
    """Matches co-located socialisers into conversations and applies updates.

    `utterance_provider(conversation, speaker_id)` returns the next utterance
    text (harness-backed). If it raises or returns None, the conversation is
    truncated (Req 10.8).
    """

    def __init__(self, relationships: Optional[Dict[Tuple[str, str], Relationship]] = None):
        # keyed by (from_id, to_id)
        self.relationships: Dict[Tuple[str, str], Relationship] = relationships or {}
        self._milestone_reached: set[Tuple[str, str]] = set()
        # seed milestone set from any pre-existing >=50 familiarity rels
        for (f, t), rel in self.relationships.items():
            if rel.familiarity >= FAMILIARITY_MILESTONE:
                self._milestone_reached.add((f, t))

    # -- matching (Req 10.1 / 10.9) ----------------------------------------
    def match_conversation(self, initiator: Agent, colocated: List[Agent],
                           in_conversation: set[str], conversation_id: str,
                           location_id: str) -> Tuple[Optional[Conversation], List[Tuple[str, str]]]:
        """Attempt to form a conversation around `initiator`'s socialise action.

        Returns (conversation_or_None, declined_pairs). declined_pairs lists
        (initiator, target) that could not start (Req 10.9).
        """
        action = initiator.state.currentAction
        declined: List[Tuple[str, str]] = []
        if action is None or action.type != ActionType.SOCIALISE:
            return None, declined
        if action.targetType != TargetType.AGENT:
            return None, declined

        target_id = action.targetId
        colo_ids = {a.id for a in colocated}
        # target must be co-located and not already in a conversation.
        if target_id not in colo_ids or target_id in in_conversation \
                or initiator.id in in_conversation:
            declined.append((initiator.id, target_id))
            return None, declined

        participants = [initiator.id, target_id]
        # up to 4 more agents at the location holding socialise->participant.
        for a in colocated:
            if len(participants) >= MAX_PARTICIPANTS:
                break
            if a.id in participants or a.id in in_conversation:
                continue
            act = a.state.currentAction
            if act is not None and act.type == ActionType.SOCIALISE \
                    and act.targetType == TargetType.AGENT \
                    and act.targetId in participants:
                participants.append(a.id)

        convo = Conversation(id=conversation_id, participants=participants,
                             location_id=location_id)
        return convo, declined

    # -- running (Req 10.2 / 10.8) -----------------------------------------
    def run_conversation(self, convo: Conversation,
                         utterance_provider: Callable[[Conversation, str], Optional[str]],
                         max_utterances: int = MAX_UTTERANCES) -> Conversation:
        """Drive alternating utterances up to max; validate <=500 chars."""
        max_u = min(MAX_UTTERANCES, max(MIN_UTTERANCES, max_utterances))
        idx = 0
        for turn in range(max_u):
            speaker = convo.participants[idx % len(convo.participants)]
            try:
                text = utterance_provider(convo, speaker)
            except Exception:
                convo.truncated = True
                break
            if text is None:
                convo.truncated = True
                break
            text = text[:MAX_UTTERANCE_CHARS]
            convo.utterances.append(Utterance(speaker=speaker, text=text))
            idx += 1
        convo.ended = True
        return convo

    # -- resolution (Req 10.4-10.7, 10.10, 10.11) --------------------------
    def resolve(self, convo: Conversation,
                familiarity_delta: Callable[[str, str], int] = None,
                sentiment_delta: Callable[[str, str], int] = None) -> ConversationOutcome:
        """Apply memory + relationship updates for an ended conversation.

        `*_delta` callables let the caller supply deterministic per-pair deltas
        (clamped to the permitted step ranges). Defaults: +1 familiarity, 0
        sentiment (minimal deterministic default).
        """
        n = len(convo.utterances)
        changes: List[Tuple[str, str, int, int]] = []
        milestones: List[Tuple[str, str]] = []

        if n < MIN_UTTERANCES:
            # No memory, no adjustment (Req 10.10).
            return ConversationOutcome(convo, False, [], [], n)

        fam_fn = familiarity_delta or (lambda a, b: 1)
        sent_fn = sentiment_delta or (lambda a, b: 0)

        for i, a in enumerate(convo.participants):
            for j, b in enumerate(convo.participants):
                if i == j:
                    continue
                dfam = max(FAMILIARITY_STEP_MIN, min(FAMILIARITY_STEP_MAX, fam_fn(a, b)))
                dsent = max(SENTIMENT_STEP_MIN, min(SENTIMENT_STEP_MAX, sent_fn(a, b)))
                rel = self._get_or_create(a, b)
                rel.familiarity = _clamp_rel(rel.familiarity + dfam)
                rel.sentiment = _clamp_rel(rel.sentiment + dsent)
                changes.append((a, b, dfam, dsent))
                if rel.familiarity >= FAMILIARITY_MILESTONE and (a, b) not in self._milestone_reached:
                    self._milestone_reached.add((a, b))
                    milestones.append((a, b))

        return ConversationOutcome(convo, True, changes, milestones, n)

    def _get_or_create(self, from_id: str, to_id: str) -> Relationship:
        key = (from_id, to_id)
        rel = self.relationships.get(key)
        if rel is None:
            rel = Relationship(from_id=from_id, to_id=to_id, familiarity=0, sentiment=0)
            self.relationships[key] = rel
        return rel

    def get_relationship(self, from_id: str, to_id: str) -> Relationship:
        return self.relationships.get((from_id, to_id),
                                      Relationship(from_id=from_id, to_id=to_id))


__all__ = [
    "Social_Engine", "Conversation", "Utterance", "ConversationOutcome",
    "MAX_PARTICIPANTS", "MIN_UTTERANCES", "MAX_UTTERANCES", "MAX_UTTERANCE_CHARS",
    "FAMILIARITY_MILESTONE", "REL_CLAMP_MIN", "REL_CLAMP_MAX",
]
