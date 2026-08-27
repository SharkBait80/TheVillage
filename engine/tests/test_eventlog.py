"""Tests for the Event_Log (Requirement 14)."""
import pytest

from village.eventlog import Event_Log, EventLogModifyError, seq20


def test_seq20_zero_padded():
    assert seq20(123) == "00000000000000000123"
    assert len(seq20(1)) == 20


def test_monotonic_sequence():
    log = Event_Log()
    e1 = log.append("2026-03-02T06:00:00+11:00", "action", "a")
    e2 = log.append("2026-03-02T06:01:00+11:00", "action", "b")
    assert e2.seq == e1.seq + 1


def test_ordering_ascending_simtime_then_realtime():
    log = Event_Log()
    log.append("2026-03-02T06:02:00+11:00", "action", "later", real_time="2026-01-01T00:00:02")
    log.append("2026-03-02T06:01:00+11:00", "action", "earlier", real_time="2026-01-01T00:00:01")
    log.append("2026-03-02T06:01:00+11:00", "action", "same-simtime-later-real", real_time="2026-01-01T00:00:05")
    res = log.query()
    descs = [e.description for e in res.entries]
    assert descs == ["earlier", "same-simtime-later-real", "later"]


def test_filter_by_category_and_agent():
    log = Event_Log()
    log.append("2026-03-02T06:00:00+11:00", "crime", "c", agents=["a1"])
    log.append("2026-03-02T06:00:00+11:00", "action", "x", agents=["a1"])
    log.append("2026-03-02T06:00:00+11:00", "crime", "c2", agents=["a2"])
    res = log.query(category="crime", agent_id="a1")
    assert len(res.entries) == 1
    assert res.entries[0].description == "c"


def test_filter_by_simtime_range():
    log = Event_Log()
    log.append("2026-03-02T06:00:00+11:00", "action", "before")
    log.append("2026-03-02T08:00:00+11:00", "action", "inside")
    log.append("2026-03-02T10:00:00+11:00", "action", "after")
    res = log.query(from_sim_time="2026-03-02T07:00:00+11:00",
                    to_sim_time="2026-03-02T09:00:00+11:00")
    assert [e.description for e in res.entries] == ["inside"]


def test_query_cap_and_more_flag():
    log = Event_Log()
    for i in range(600):
        log.append(f"2026-03-02T06:{i%60:02d}:00+11:00", "action", f"e{i}")
    res = log.query(limit=500)
    assert len(res.entries) == 500
    assert res.more is True
    res2 = log.query(cursor=500, limit=500)
    assert len(res2.entries) == 100
    assert res2.more is False


def test_empty_result_when_no_match():
    log = Event_Log()
    log.append("2026-03-02T06:00:00+11:00", "action", "x")
    res = log.query(category="crime")
    assert res.entries == []
    assert res.more is False


def test_append_only_guard():
    log = Event_Log()
    log.append("2026-03-02T06:00:00+11:00", "action", "x")
    with pytest.raises(EventLogModifyError):
        log.modify()
    with pytest.raises(EventLogModifyError):
        log.remove()


def test_decision_trail():
    log = Event_Log()
    e = log.append("2026-03-02T06:00:00+11:00", "action", "decided",
                   agents=["a1"],
                   detail={"perceptionInput": {"needs": {}},
                           "retrievedMemoryIds": ["m1", "m2"],
                           "action": {"type": "eat"}})
    trail = log.decision_trail(e.seq)
    assert trail is not None
    assert trail["retrievedMemoryIds"] == ["m1", "m2"]
    assert trail["action"]["type"] == "eat"


def test_decision_trail_missing_returns_none():
    log = Event_Log()
    assert log.decision_trail(999) is None


def test_summary_counts_current_day():
    log = Event_Log()
    day_start = "2026-03-02T00:00:00+11:00"
    now = "2026-03-02T23:59:00+11:00"
    log.append("2026-03-02T10:00:00+11:00", "crime", "theft")
    log.append("2026-03-02T11:00:00+11:00", "conversation", "conversation-ended chat",
               detail={"kind": "conversation-ended"})
    # previous day event excluded
    log.append("2026-03-01T10:00:00+11:00", "crime", "old crime")
    summary = log.summary(now, day_start,
                          legal_counts={"clear": 20, "suspected": 3},
                          employment_counts={"employed": 18})
    assert summary["crimeCount"] == 1
    assert summary["conversationCount"] == 1
    assert summary["legalStatusCounts"]["clear"] == 20
    assert summary["legalStatusCounts"]["detained"] == 0
    assert summary["employmentStatusCounts"]["employed"] == 18
