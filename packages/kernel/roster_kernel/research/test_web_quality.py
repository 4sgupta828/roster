"""Unit tests for the open-web quality screen (LLM-owned).

Contract: `None` = could-not-judge (caller fails safe); `[]` = judged-nothing-kept
or nothing-to-judge; `list` = kept hits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from roster_kernel.research.web_quality import (
    _Verdict,
    _VerdictP,
    _Verdicts,
    screen_open_web_hits,
)


# --- minimal fakes -----------------------------------------------------------

@dataclass
class _FakeHit:
    """A BlockHit-like item carrying only the fields the screen reads.

    Includes a `facets` dict (like the real frozen BlockHit) so provenance stamping via
    `dataclasses.replace(hit, facets={...})` has a field to rebuild.
    """
    document_id: str = ""
    document_title: str = ""
    text: str = ""
    facets: dict = field(default_factory=dict)


@dataclass
class _FakeResult:
    parsed: object
    output_tokens: int = 0


@dataclass
class _FakeBudget:
    exhausted: bool = False
    charges: list = field(default_factory=list)

    def charge(self, *, calls: int = 1, tokens: int = 0) -> None:
        self.charges.append((calls, tokens))


class _ScriptedLLM:
    """Returns a fixed _Verdicts (or raises) and records the call."""

    def __init__(self, verdicts=None, *, raises: Exception | None = None, output_tokens: int = 42):
        self._verdicts = verdicts or []
        self._raises = raises
        self._output_tokens = output_tokens
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        # Build the verdict container from the schema the screen actually requested (so a
        # provenance-enabled call gets `_VerdictsP`, a plain call gets `_Verdicts`).
        return _FakeResult(parsed=response_format(verdicts=self._verdicts), output_tokens=self._output_tokens)


_PROMPT = "judge these pages (domain prompt injected here)"


def _hits(n: int) -> list[_FakeHit]:
    return [_FakeHit(document_id=f"http://x/{i}", document_title=f"t{i}", text=f"body {i}") for i in range(n)]


# --- tests -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keep_drop_filtering_by_index():
    hits = _hits(3)
    llm = _ScriptedLLM([
        _Verdict(index=0, keep=True),
        _Verdict(index=1, keep=False),
        _Verdict(index=2, keep=True),
    ])
    budget = _FakeBudget()
    out = await screen_open_web_hits(hits, question="q", llm=llm, prompt=_PROMPT, budget=budget)
    assert out == [hits[0], hits[2]]
    assert llm.calls == 1
    assert budget.charges == [(1, 42)]  # charged once with output_tokens


@pytest.mark.asyncio
async def test_out_of_range_indices_ignored():
    hits = _hits(2)
    llm = _ScriptedLLM([
        _Verdict(index=0, keep=True),
        _Verdict(index=5, keep=True),   # out of range → ignored
        _Verdict(index=-1, keep=True),  # out of range → ignored
    ])
    out = await screen_open_web_hits(hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget())
    assert out == [hits[0]]


@pytest.mark.asyncio
async def test_empty_hits_returns_empty():
    # Nothing to judge is NOT a failure — empty input legitimately yields [] (not None).
    llm = _ScriptedLLM([_Verdict(index=0, keep=True)])
    budget = _FakeBudget()
    out = await screen_open_web_hits([], question="q", llm=llm, prompt=_PROMPT, budget=budget)
    assert out == []
    assert llm.calls == 0
    assert budget.charges == []


@pytest.mark.asyncio
async def test_judged_all_drop_returns_empty_list():
    # A judge that ran and dropped everything → [] (respected), NOT None (can't-judge).
    hits = _hits(3)
    llm = _ScriptedLLM([
        _Verdict(index=0, keep=False),
        _Verdict(index=1, keep=False),
        _Verdict(index=2, keep=False),
    ])
    budget = _FakeBudget()
    out = await screen_open_web_hits(hits, question="q", llm=llm, prompt=_PROMPT, budget=budget)
    assert out == []
    assert llm.calls == 1
    assert budget.charges == [(1, 42)]   # the judge DID run and was charged


@pytest.mark.asyncio
async def test_llm_none_returns_none():
    # Could not judge (no judge) → None so the caller fails safe.
    out = await screen_open_web_hits(_hits(2), question="q", llm=None, prompt=_PROMPT, budget=_FakeBudget())
    assert out is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [None, "", "   "])
async def test_prompt_missing_returns_none(prompt):
    # Blank/missing prompt → could not judge → None.
    llm = _ScriptedLLM([_Verdict(index=0, keep=True)])
    out = await screen_open_web_hits(_hits(2), question="q", llm=llm, prompt=prompt, budget=_FakeBudget())
    assert out is None
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_budget_exhausted_returns_none():
    # Exhausted budget → could not judge → None; never spent.
    llm = _ScriptedLLM([_Verdict(index=0, keep=True)])
    budget = _FakeBudget(exhausted=True)
    out = await screen_open_web_hits(_hits(2), question="q", llm=llm, prompt=_PROMPT, budget=budget)
    assert out is None
    assert llm.calls == 0           # never spent
    assert budget.charges == []


@pytest.mark.asyncio
async def test_llm_raises_returns_none():
    # ANY error → could not judge → None so the caller fails safe.
    llm = _ScriptedLLM(raises=RuntimeError("boom"))
    out = await screen_open_web_hits(_hits(3), question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget())
    assert out is None


@pytest.mark.asyncio
async def test_emit_provenance_stamps_web_role_on_kept_hits():
    # emit_provenance=True: each KEPT hit gets a generic `web_role` facet from its verdict's role.
    hits = _hits(3)
    llm = _ScriptedLLM([
        _VerdictP(index=0, keep=True, provenance="official"),
        _VerdictP(index=1, keep=False, provenance="social"),   # dropped → not in output
        _VerdictP(index=2, keep=True, provenance="independent_analysis"),
    ])
    out = await screen_open_web_hits(
        hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget(), emit_provenance=True)
    assert len(out) == 2
    assert out[0].facets.get("web_role") == "official"
    assert out[1].facets.get("web_role") == "independent_analysis"


@pytest.mark.asyncio
async def test_emit_provenance_unknown_role_leaves_web_role_unset():
    # A missing/unknown provenance → NO web_role stamp (fail-safe), but the hit is still kept.
    hits = _hits(2)
    llm = _ScriptedLLM([
        _VerdictP(index=0, keep=True, provenance=""),          # blank → no stamp
        _VerdictP(index=1, keep=True, provenance="mystery"),   # unknown → no stamp
    ])
    out = await screen_open_web_hits(
        hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget(), emit_provenance=True)
    assert len(out) == 2
    assert "web_role" not in out[0].facets
    assert "web_role" not in out[1].facets


@pytest.mark.asyncio
async def test_emit_provenance_false_leaves_hits_unstamped():
    # OFF path (default): the plain `_Verdicts` schema is requested (no provenance field) and no
    # web_role is stamped — byte-identical to pre-T3 behavior.
    hits = _hits(2)
    llm = _ScriptedLLM([
        _Verdict(index=0, keep=True),
        _Verdict(index=1, keep=True),
    ])
    out = await screen_open_web_hits(
        hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget())  # emit_provenance defaults False
    assert out == [hits[0], hits[1]]
    assert all("web_role" not in h.facets for h in out)


@pytest.mark.asyncio
async def test_provenance_preserves_existing_facets():
    # Stamping web_role must not clobber pre-existing facets on the kept hit.
    hits = [_FakeHit(document_id="http://a", document_title="t", text="b", facets={"lang": "en"})]
    llm = _ScriptedLLM([_VerdictP(index=0, keep=True, provenance="expert_opinion")])
    out = await screen_open_web_hits(
        hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget(), emit_provenance=True)
    assert out[0].facets == {"lang": "en", "web_role": "expert_opinion"}


@pytest.mark.asyncio
async def test_duplicate_urls_deduped():
    hits = [
        _FakeHit(document_id="http://same", document_title="a", text="body a"),
        _FakeHit(document_id="http://same", document_title="b", text="body b"),
        _FakeHit(document_id="http://other", document_title="c", text="body c"),
    ]
    # after dedup: index 0 = first "same", index 1 = "other"
    llm = _ScriptedLLM([_Verdict(index=0, keep=True), _Verdict(index=1, keep=True)])
    out = await screen_open_web_hits(hits, question="q", llm=llm, prompt=_PROMPT, budget=_FakeBudget())
    assert out == [hits[0], hits[2]]
