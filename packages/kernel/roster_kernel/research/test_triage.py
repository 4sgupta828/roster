"""run_triage_turn schema selection (Intake v2): v1 calls stay byte-identical (exact
response_format + message-payload assertions), schema_v2 selects TriageTurnV2 and its generic
fields (register / case_facts / retrieval_terms) flow through, force_ready coercion and the
fail-safe fallback behave identically under both schemas."""
import asyncio

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.triage import TriageTurn, TriageTurnV2, run_triage_turn


class FakeLLM:
    def __init__(self, turn=None, raise_it=False):
        self._turn = turn
        self._raise = raise_it
        self.calls = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls.append({"system": system, "messages": messages,
                           "response_format": response_format, "max_tokens": max_tokens})
        if self._raise:
            raise RuntimeError("provider down")
        return LLMResult(parsed=self._turn, model="fake")


TRANSCRIPT = [{"role": "user", "text": "my mom is dizzy"},
              {"role": "assistant", "text": "Which meds does she take?"},
              {"role": "user", "text": "Aricept"}]


def _run(llm, *, schema_v2=None, force_ready=False, transcript=TRANSCRIPT):
    kw = {} if schema_v2 is None else {"schema_v2": schema_v2}
    return asyncio.run(run_triage_turn(llm=llm, triage_prompt="DIRECTIVE",
                                       transcript=transcript, force_ready=force_ready, **kw))


# ---------------------------------------------------------------- v1 byte-identical

def test_v1_default_call_uses_v1_schema_exactly():
    llm = FakeLLM(TriageTurn(status="ask", message="How long?"))
    turn = _run(llm)                                   # schema_v2 not passed — the default path
    assert llm.calls[0]["response_format"] is TriageTurn
    assert llm.calls[0]["system"] == "DIRECTIVE"
    assert llm.calls[0]["messages"] == [
        {"role": "user", "content": "my mom is dizzy"},
        {"role": "assistant", "content": "Which meds does she take?"},
        {"role": "user", "content": "Aricept"}]
    assert type(turn) is TriageTurn
    assert "register" not in turn.model_dump()         # v1 responses carry no v2 fields


def test_v1_explicit_false_is_the_same_call():
    a, b = (FakeLLM(TriageTurn(status="ask", message="q")) for _ in range(2))
    _run(a)
    _run(b, schema_v2=False)
    assert a.calls == b.calls                          # byte-identical payload either way


# ---------------------------------------------------------------- v2 schema + fields

def test_v2_selects_v2_schema_and_fields_flow_through():
    parsed = TriageTurnV2(
        status="ask", message="So I can search the right evidence, when did this start?",
        register="case",
        case_facts=[{"category": "core-issue", "text": "dizziness"},
                    {"category": "medications", "text": "donepezil (Aricept)"}],
        retrieval_terms=["donepezil", "dizziness"])
    llm = FakeLLM(parsed)
    turn = _run(llm, schema_v2=True)
    assert llm.calls[0]["response_format"] is TriageTurnV2
    d = turn.model_dump()
    assert d["register"] == "case"
    assert d["case_facts"] == [{"category": "core-issue", "text": "dizziness"},
                               {"category": "medications", "text": "donepezil (Aricept)"}]
    assert d["retrieval_terms"] == ["donepezil", "dizziness"]


def test_v2_defaults_are_generic():
    t = TriageTurnV2()
    assert t.register == "fact" and t.case_facts == [] and t.retrieval_terms == []
    assert isinstance(t, TriageTurn)                   # v1 consumers still see a TriageTurn


# ---------------------------------------------------------------- force_ready coercion

def test_force_ready_coerces_and_backfills_refined_v2():
    llm = FakeLLM(TriageTurnV2(status="ask", message="one more?", register="case",
                               understood_problem="mom dizzy on Aricept"))
    turn = _run(llm, schema_v2=True, force_ready=True)
    assert turn.status == "ready"
    assert turn.refined_question == "mom dizzy on Aricept"   # backfilled from understood_problem
    # the caller-cap instruction is appended to the message payload
    assert "do NOT ask another question" in llm.calls[0]["messages"][-1]["content"]


def test_force_ready_backfills_from_last_user_when_nothing_understood():
    llm = FakeLLM(TriageTurn(status="ask", message="one more?"))
    turn = _run(llm, force_ready=True)
    assert turn.status == "ready" and turn.refined_question == "Aricept"


# ---------------------------------------------------------------- fail-safe fallback

def test_fallback_shape_matches_selected_schema():
    v1 = _run(FakeLLM(raise_it=True))
    assert type(v1) is TriageTurn and v1.status == "ready" and v1.refined_question == "Aricept"
    v2 = _run(FakeLLM(raise_it=True), schema_v2=True)
    assert type(v2) is TriageTurnV2 and v2.status == "ready"   # stable v2 response shape on error
    assert v2.register == "fact" and v2.case_facts == [] and v2.retrieval_terms == []


def test_empty_transcript_returns_selected_schema():
    llm = FakeLLM()
    assert type(_run(llm, transcript=[])) is TriageTurn
    assert type(_run(llm, schema_v2=True, transcript=[])) is TriageTurnV2
    assert llm.calls == []                             # no LLM call on an empty transcript
