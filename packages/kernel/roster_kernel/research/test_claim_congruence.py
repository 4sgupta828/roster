"""Evidence Contract stage 2 — the claim-congruence flag (ROSTER_CLAIM_CONGRUENCE).

ON: ONE unified batched BINDING judge covers all three claim paths (loop-emitted, claims-first,
fallback-grounder). Per item it returns {entailed, on_subject, kind_ok}; enforcement: off-subject →
DROP (traced), not-entailed → DROP, kind-mismatch → KEEP + demote + annotate, judge failure → KEEP +
"unjudged" (never dropped). OFF: stage-1-only prompts and enforcement, byte-identical. Also covers
the BudgetState-honesty re-plan (claims-first/binding/fallback charges + the raised ceiling).
Offline throughout — scripted LLMs / fakes, no network (mirrors test_evidence_identity.py)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import DEFAULT_MAX_LLM_CALLS, BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer, VerifiedClaim, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

_TITLE = "EXAMPLE COMPOUND TABLET"
_TAG = f"⟨{_TITLE} — dailymed⟩"
_BLOCK_TEXT = ("Alpha metric was 11 percent. Beta metric was 22 percent. Gamma metric was "
               "33 percent. Delta metric was 44 percent. Loop metric was 55 percent.")


def _source() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
        document_title=_TITLE, source_key="dailymed"))
    return src


class RecordingLLM:
    """Returns pre-scripted parsed objects in order AND records every prompt it saw."""
    def __init__(self, steps):
        self._steps = list(steps)
        self.prompts: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.prompts.append(messages[0]["content"])
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


def _loop_script(compose: bool = True):
    """search → answer (one verified loop claim) [→ compose]."""
    steps = [
        AgentStep(action="search", query="metric values"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="loop claim ok", atom_id="a1", quote="Loop metric was 55 percent")]),
    ]
    if compose:
        steps.append(ComposedAnswer(answer="Metrics were reported [1]."))
    return steps


class _FakeJson:
    """Records the (system, user) of every complete_json call; returns a scripted payload."""
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []

    async def complete_json(self, *, model, system, user):
        self.calls.append((model, system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


# ---- the binding judge itself (claims_first.entail_claims, congruence mode) -----------------

_CLAIMS = [{"text": "c1", "atom_id": "a1", "quote": "q1"},
           {"text": "c2", "atom_id": "a2", "quote": "q2"}]


def test_binding_items_carry_claim_source_kind_and_quote():
    from roster_kernel.research.claims_first import entail_claims
    cl = _FakeJson({"verdicts": []})
    asyncio.run(entail_claims(claims=_CLAIMS, client=cl, congruence=True,
                              tags=[_TAG, ""], kinds=["finding_type_a", ""]))
    _, system, user = cl.calls[0]
    assert "evidence-binding judge" in system                 # the BINDING system prompt is used
    assert '"entailed"' in system and '"on_subject"' in system and '"kind_ok"' in system
    # item 0: SOURCE = tag + the structural kind riding along as data
    assert f"ITEM 0\nCLAIM: c1\nSOURCE: {_TAG} [kind of finding: finding_type_a]\nQUOTE: q1" in user
    # item 1: no tag + no kind → no SOURCE line at all (judge told to treat as on-subject)
    assert "ITEM 1\nCLAIM: c2\nQUOTE: q2" in user


def test_binding_verdicts_parsed_with_conservative_defaults():
    from roster_kernel.research.claims_first import entail_claims
    cl = _FakeJson({"verdicts": [
        {"i": 0, "entailed": True, "on_subject": False, "kind_ok": True},
        {"i": 1, "entailed": True},                            # on_subject/kind_ok omitted → True
    ]})
    out = asyncio.run(entail_claims(claims=_CLAIMS, client=cl, congruence=True))
    assert out == [{"entailed": True, "on_subject": False, "kind_ok": True},
                   {"entailed": True, "on_subject": True, "kind_ok": True}]


def test_binding_missing_verdict_and_missing_entailed_stay_conservative():
    from roster_kernel.research.claims_first import entail_claims
    # only item 0 ruled, and WITHOUT "entailed" → entailed defaults False; item 1 unruled → None
    cl = _FakeJson({"verdicts": [{"i": 0, "on_subject": True}]})
    out = asyncio.run(entail_claims(claims=_CLAIMS, client=cl, congruence=True))
    assert out[0] == {"entailed": False, "on_subject": True, "kind_ok": True}
    assert out[1] is None


def test_binding_no_client_returns_all_none(monkeypatch):
    from roster_kernel.research.claims_first import entail_claims
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = asyncio.run(entail_claims(claims=_CLAIMS, congruence=True))
    assert out == [None, None]                                 # unavailable ≠ False: caller annotates


def test_binding_errored_batch_returns_none_items():
    from roster_kernel.research.claims_first import entail_claims
    cl = _FakeJson(RuntimeError("judge down"))
    out = asyncio.run(entail_claims(claims=_CLAIMS, client=cl, congruence=True))
    assert out == [None, None]


def test_congruence_off_is_stage1_byte_identical():
    from roster_kernel.research.claims_first import entail_claims
    cl = _FakeJson({"verdicts": [{"i": 0, "ok": True}, {"i": 1, "ok": True}]})
    out = asyncio.run(entail_claims(claims=_CLAIMS, client=cl, tags=[_TAG, ""]))
    assert out == [True, True]                                 # plain bools, as stage 1
    _, system, user = cl.calls[0]
    assert "entailment judge" in system and "evidence-binding" not in system
    assert f"ITEM 0\nCLAIM: c1\nSOURCE: {_TAG}\nQUOTE: q1" in user   # exact stage-1 rendering
    assert "kind of finding" not in user


# ---- run_react enforcement: claims-first path ------------------------------------------------

_VERDICTS = {
    "loop claim ok":      {"entailed": True, "on_subject": True, "kind_ok": True},
    "cand ok":            {"entailed": True, "on_subject": True, "kind_ok": True},
    "cand off subject":   {"entailed": True, "on_subject": False, "kind_ok": True},
    "cand kind mismatch": {"entailed": True, "on_subject": True, "kind_ok": False},
    "cand not entailed":  {"entailed": False, "on_subject": True, "kind_ok": True},
}

_CANDIDATES = [  # kind-mismatch FIRST so the demotion partition provably moves it behind "cand ok"
    {"text": "cand kind mismatch", "atom_id": "a1", "quote": "Gamma metric was 33 percent"},
    {"text": "cand ok", "atom_id": "a1", "quote": "Alpha metric was 11 percent"},
    {"text": "cand off subject", "atom_id": "a1", "quote": "Beta metric was 22 percent"},
    {"text": "cand not entailed", "atom_id": "a1", "quote": "Delta metric was 44 percent"},
]


def _patch_judges(monkeypatch, captured: dict):
    """Fake extract/entail at the claims_first module (imported at call time by run_react)."""
    import roster_kernel.research.claims_first as cf

    async def fake_extract(**kw):
        captured.setdefault("extract", []).append(kw)
        return list(_CANDIDATES)

    async def fake_entail(**kw):
        captured.setdefault("entail", []).append(kw)
        if kw.get("congruence"):
            return [_VERDICTS[c["text"]] for c in kw["claims"]]
        return [True] * len(kw["claims"])

    monkeypatch.setattr(cf, "extract_claims", fake_extract)
    monkeypatch.setattr(cf, "entail_claims", fake_entail)


def _run(llm, *, monkeypatch=None, captured=None, key=True, **kw):
    if monkeypatch is not None:
        if key:
            monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        else:
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        if captured is not None:
            _patch_judges(monkeypatch, captured)
    budget = kw.pop("budget", BudgetState(max_calls=30))
    res = asyncio.run(run_react(
        question="what were the metric values?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="A", budget=budget, **kw))
    return res, budget


def test_binding_enforced_drop_demote_and_trace(monkeypatch):
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    res, _ = _run(llm, monkeypatch=monkeypatch, captured=captured,
                  claims_first=True, claim_congruence=True, collect_diagnostics=True)
    # off-subject and not-entailed dropped; kind-mismatch kept + annotated + DEMOTED to the back
    assert [vc.text for vc in res.verified_claims] == ["loop claim ok", "cand ok",
                                                       "cand kind mismatch"]
    assert [vc.congruence_note for vc in res.verified_claims] == ["", "", "kind_mismatch"]
    # the off-subject drop is in the diag trace with its reason (the misattribution observable)
    drops = [t for t in res.diagnostics["trace"] if t.get("action") == "congruence_drop"]
    assert drops == [{"action": "congruence_drop", "reason": "off_subject",
                      "origin": "claims_first", "claim": "cand off subject", "title": _TITLE}]
    assert res.diagnostics["congruence"] == {"judged": 5, "off_subject": 1, "not_entailed": 1,
                                             "kind_mismatch": 1, "unjudged": 0}
    # compose renders the annotation on the demoted finding only — generic wording
    compose = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
    assert "[3] cand kind mismatch" in compose and "[kind-mismatch]" in compose
    assert "[unjudged]" not in compose
    assert compose.count("[kind-mismatch]") == 1


def test_loop_claims_routed_through_binding_judge(monkeypatch):
    # Loop-emitted claims previously received NO entailment at all — under the flag they go through
    # the SAME batched binding judge (one extra call), with tags + kinds even when stage 1 is off.
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    _run(llm, monkeypatch=monkeypatch, captured=captured, claim_congruence=True)
    assert len(captured["entail"]) == 1                        # exactly ONE extra batched call
    call = captured["entail"][0]
    assert call["congruence"] is True
    assert [c["text"] for c in call["claims"]] == ["loop claim ok"]
    assert call["tags"] == [_TAG]                              # SOURCE identity always rides along
    assert call["kinds"] == [""]                               # no vertical classifier → empty kind


def test_loop_claim_off_subject_is_dropped(monkeypatch):
    llm = RecordingLLM([
        AgentStep(action="search", query="metric values"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="cand off subject", atom_id="a1", quote="Beta metric was 22 percent")]),
        # dropped-to-zero claims → extract-recovery re-asks (3), then no compose
        AgentStep(action="answer", claims=[]),
        AgentStep(action="answer", claims=[]),
        AgentStep(action="answer", claims=[]),
    ])
    import roster_kernel.research.fallback_grounder as fg

    async def no_ground(**kw):
        return []
    monkeypatch.setattr(fg, "ground_claimless", no_ground)
    captured2: dict = {}
    res, _ = _run(llm, monkeypatch=monkeypatch, captured=captured2, claim_congruence=True)
    assert not res.grounded and res.verified_claims == []


def test_judge_unavailable_keeps_claims_annotated_unjudged(monkeypatch):
    # No key → the binding judge never runs; loop claims are KEPT with note "unjudged" — never
    # dropped on judge failure, never a keyword fallback (Rule 18 fail-safe).
    llm = RecordingLLM(_loop_script())
    res, _ = _run(llm, monkeypatch=monkeypatch, key=False, claim_congruence=True)
    assert [vc.text for vc in res.verified_claims] == ["loop claim ok"]
    assert res.verified_claims[0].congruence_note == "unjudged"
    compose = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
    assert "[1] loop claim ok" in compose and "[unjudged]" in compose


def test_judge_error_keeps_claims_annotated_unjudged(monkeypatch):
    import roster_kernel.research.claims_first as cf

    async def broken_entail(**kw):
        raise RuntimeError("judge down")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cf, "entail_claims", broken_entail)
    llm = RecordingLLM(_loop_script())
    res, _ = _run(llm, claim_congruence=True)
    assert [vc.congruence_note for vc in res.verified_claims] == ["unjudged"]


# ---- OFF path: byte-identical to stage 1 -----------------------------------------------------

def test_off_path_entailment_and_findings_are_stage1_identical(monkeypatch):
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    res, _ = _run(llm, monkeypatch=monkeypatch, captured=captured, claims_first=True)
    # entail called WITHOUT the congruence/kinds kwargs (stage-1 signature exactly)
    call = captured["entail"][0]
    assert "congruence" not in call and "kinds" not in call
    assert call["tags"] is None                                # evidence_identity off → None
    assert len(captured["entail"]) == 1                        # and NO extra loop-binding call
    # compose findings render exactly as stage 1 — no bracket annotations anywhere
    compose = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
    assert ('[1] loop claim ok  (quote: "Loop metric was 55 percent" — source: dailymed)\n'
            in compose + "\n")
    assert "[kind-mismatch]" not in compose and "[unjudged]" not in compose
    assert all(vc.congruence_note == "" for vc in res.verified_claims)


def test_verified_claim_note_defaults_empty():
    assert VerifiedClaim("t", "a1", "q").congruence_note == ""


# ---- BudgetState honesty: charges + the raised ceiling ---------------------------------------

def test_default_ceiling_raised_to_80():
    # The re-plan: previously-unmetered calls are now charged, ceiling doubled to compensate.
    assert DEFAULT_MAX_LLM_CALLS == 80
    assert BudgetState().max_calls == 80


def test_claims_first_batches_charged_when_key_present(monkeypatch):
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    res, budget = _run(llm, monkeypatch=monkeypatch, captured=captured, claims_first=True)
    # 2 planner steps + 1 extract batch (1 atom → ceil(1/10)=1) + 1 entail batch + 1 compose = 5
    assert budget.spent_calls == 5
    assert res.grounded


def test_claims_first_not_charged_without_key(monkeypatch):
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    _, budget = _run(llm, monkeypatch=monkeypatch, captured=captured, key=False, claims_first=True)
    assert budget.spent_calls == 3                             # planner ×2 + compose only


def test_binding_batch_charged_under_flag(monkeypatch):
    captured: dict = {}
    llm = RecordingLLM(_loop_script())
    _, budget = _run(llm, monkeypatch=monkeypatch, captured=captured, claim_congruence=True)
    # 2 planner + 1 binding batch (1 loop claim → ceil(1/12)=1) + 1 compose = 4
    assert budget.spent_calls == 4


def test_fallback_grounder_charged_when_key_present(monkeypatch):
    import roster_kernel.research.fallback_grounder as fg

    async def no_ground(**kw):
        return []
    monkeypatch.setattr(fg, "ground_claimless", no_ground)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = RecordingLLM([
        AgentStep(action="search", query="metric values"),
        AgentStep(action="answer", claims=[]),        # abstains → 3 extract-recovery re-asks
        AgentStep(action="answer", claims=[]),
        AgentStep(action="answer", claims=[]),
        AgentStep(action="answer", claims=[]),
    ])
    _, budget = _run(llm)
    # 5 planner asks (search + answer + 3 recoveries) + 1 fallback-grounder charge = 6
    assert budget.spent_calls == 6


def test_a_previous_default_run_fits_the_new_ceiling(monkeypatch):
    # The arithmetic of the re-plan: a worst-case previously-in-budget run (~40 charged planner/
    # compose calls) plus the worst-case newly-charged ≈29 (14 extract + 10 entail + 3 binding +
    # 1 fallback + 1 frame-repair) stays under the new default ceiling of 80.
    assert 40 + 29 <= DEFAULT_MAX_LLM_CALLS
