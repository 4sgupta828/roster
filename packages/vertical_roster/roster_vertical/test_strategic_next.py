"""Strategic next-questions (ROSTER_STRATEGIC_NEXT): CEO/VC/CTO lens-tagged follow-ups + the
consolidation (the in-answer 'What to explore next' list is dropped so next-questions live in ONE
place). Also locks the kernel suggest contract: [{question, tag}]."""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace


def _reload(flag: str, monkeypatch):
    monkeypatch.setenv("ROSTER_STRATEGIC_NEXT", flag)
    import roster_vertical.suggest as S
    import roster_vertical.reasoned as R
    importlib.reload(S)
    importlib.reload(R)
    return S, R


def test_flag_off_is_the_original_suggester_and_keeps_in_answer_questions(monkeypatch) -> None:
    S, R = _reload("", monkeypatch)
    assert S.strategic_next_on() is False
    p = S.tech_suggest_prompt()
    assert "CEO" not in p and "diligence" in p                 # the original prompt
    assert "What to explore next" in R.tech_closing_block()    # in-answer questions retained


def test_flag_on_swaps_to_strategic_and_drops_in_answer_questions(monkeypatch) -> None:
    S, R = _reload("1", monkeypatch)
    assert S.strategic_next_on() is True
    p = S.tech_suggest_prompt()
    assert "CEO" in p and "VC" in p and "CTO" in p             # the strategic three-lens prompt
    assert "What to explore next" not in R.tech_closing_block()  # consolidated to the bottom block
    assert "Bottom line" in R.tech_closing_block()             # the synthesis is still required
    _reload("", monkeypatch)                                   # restore for other tests


def test_kernel_suggest_returns_question_tag_dicts(monkeypatch) -> None:
    from roster_kernel.research.suggest import suggest_followups

    class _LLM:
        async def complete(self, **kw):
            return SimpleNamespace(parsed=SimpleNamespace(questions=[
                SimpleNamespace(question="How durable is the moat vs hyperscalers?", tag="CEO"),
                SimpleNamespace(question="What's the realistic TAM and timing?", tag="VC"),
                SimpleNamespace(question="How durable is the moat vs hyperscalers?", tag="CEO"),  # dup
            ]))

    out = asyncio.run(suggest_followups(llm=_LLM(), suggest_prompt="x", question="q", answer="a"))
    assert out == [
        {"question": "How durable is the moat vs hyperscalers?", "tag": "CEO"},
        {"question": "What's the realistic TAM and timing?", "tag": "VC"},
    ]
