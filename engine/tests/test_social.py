"""Tests for the Social_Engine (Requirement 10)."""
from village.models import (Action, ActionType, Agent, AgentState, Persona,
                            Relationship, TargetType)
from village.social import (Conversation, MAX_PARTICIPANTS, Social_Engine,
                            Utterance)


def make_agent(aid, target=None):
    state = AgentState(lat=-37.8, lon=144.9, presentLocationId="loc_x",
                       needs={"hunger": 70, "energy": 70, "social": 70, "fun": 70})
    if target is not None:
        state.currentAction = Action(type=ActionType.SOCIALISE,
                                     targetType=TargetType.AGENT,
                                     targetId=target, expectedDurationMin=10)
    persona = Persona(name=aid, age=30, occupation="x", traits=["warm"],
                      background="b", homeLocationId="loc_home")
    return Agent(id=aid, persona=persona, state=state)


def test_match_two_agents():
    eng = Social_Engine()
    a = make_agent("a1", target="a2")
    b = make_agent("a2", target="a1")
    convo, declined = eng.match_conversation(a, [a, b], set(), "c1", "loc_x")
    assert convo is not None
    assert set(convo.participants) == {"a1", "a2"}
    assert declined == []


def test_match_declined_when_target_not_colocated():
    eng = Social_Engine()
    a = make_agent("a1", target="a2")
    convo, declined = eng.match_conversation(a, [a], set(), "c1", "loc_x")
    assert convo is None
    assert ("a1", "a2") in declined


def test_match_declined_when_target_in_conversation():
    eng = Social_Engine()
    a = make_agent("a1", target="a2")
    b = make_agent("a2", target="a1")
    convo, declined = eng.match_conversation(a, [a, b], {"a2"}, "c1", "loc_x")
    assert convo is None
    assert ("a1", "a2") in declined


def test_max_six_participants():
    eng = Social_Engine()
    agents = [make_agent(f"a{i}", target="a0") for i in range(8)]
    agents[0].state.currentAction = Action(type=ActionType.SOCIALISE,
                                           targetType=TargetType.AGENT,
                                           targetId="a1", expectedDurationMin=10)
    convo, _ = eng.match_conversation(agents[0], agents, set(), "c1", "loc_x")
    assert convo is not None
    assert len(convo.participants) <= MAX_PARTICIPANTS


def test_run_conversation_alternates_and_caps():
    eng = Social_Engine()
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x")
    provider = lambda c, spk: f"hi from {spk}"
    eng.run_conversation(convo, provider, max_utterances=10)
    assert convo.ended
    assert 2 <= len(convo.utterances) <= 10
    # alternating speakers
    assert convo.utterances[0].speaker == "a1"
    assert convo.utterances[1].speaker == "a2"


def test_utterance_truncated_to_500_chars():
    eng = Social_Engine()
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x")
    provider = lambda c, spk: "x" * 1000
    eng.run_conversation(convo, provider, max_utterances=2)
    assert all(len(u.text) <= 500 for u in convo.utterances)


def test_provider_error_truncates_conversation():
    eng = Social_Engine()
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x")

    def provider(c, spk):
        if len(c.utterances) >= 1:
            raise RuntimeError("model timeout")
        return "hello"

    eng.run_conversation(convo, provider, max_utterances=10)
    assert convo.truncated
    assert len(convo.utterances) == 1


def test_resolve_adjusts_and_clamps_relationships():
    eng = Social_Engine()
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x",
                         utterances=[Utterance("a1", "hi"), Utterance("a2", "hey")])
    outcome = eng.resolve(convo, familiarity_delta=lambda a, b: 10,
                          sentiment_delta=lambda a, b: 20)
    assert outcome.memory_written
    rel = eng.get_relationship("a1", "a2")
    assert rel.familiarity == 10
    assert rel.sentiment == 20


def test_sentiment_clamped_to_100():
    rels = {("a1", "a2"): Relationship("a1", "a2", familiarity=95, sentiment=95)}
    eng = Social_Engine(rels)
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x",
                         utterances=[Utterance("a1", "hi"), Utterance("a2", "hey")])
    eng.resolve(convo, familiarity_delta=lambda a, b: 10, sentiment_delta=lambda a, b: 20)
    rel = eng.get_relationship("a1", "a2")
    assert rel.familiarity == 100  # clamped
    assert rel.sentiment == 100


def test_milestone_fires_once():
    rels = {("a1", "a2"): Relationship("a1", "a2", familiarity=45, sentiment=0)}
    eng = Social_Engine(rels)
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x",
                         utterances=[Utterance("a1", "hi"), Utterance("a2", "hey")])
    out1 = eng.resolve(convo, familiarity_delta=lambda a, b: 10)
    # a1->a2 crosses 50 => milestone; a2->a1 also crosses (started 0? no, only a1->a2 preset)
    milestones = out1.milestones
    assert ("a1", "a2") in milestones
    # second conversation: no repeat milestone for a1->a2
    convo2 = Conversation(id="c2", participants=["a1", "a2"], location_id="loc_x",
                          utterances=[Utterance("a1", "hi"), Utterance("a2", "hey")])
    out2 = eng.resolve(convo2, familiarity_delta=lambda a, b: 5)
    assert ("a1", "a2") not in out2.milestones


def test_fewer_than_two_utterances_no_change():
    eng = Social_Engine()
    convo = Conversation(id="c1", participants=["a1", "a2"], location_id="loc_x",
                         utterances=[Utterance("a1", "hi")])
    outcome = eng.resolve(convo)
    assert not outcome.memory_written
    assert outcome.relationship_changes == []
    # no relationship created
    assert eng.get_relationship("a1", "a2").familiarity == 0
