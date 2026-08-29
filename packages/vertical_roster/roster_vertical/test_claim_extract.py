"""Offline fixture tests for the typed grounded-claim extractor CORE (Task 2a).

NO DB, NO corpus, NO network: a FAKE LLM client (scripted `complete_json`, mirroring
`test_claim_congruence._FakeJson`) supplies the extraction JSON, and `entail_claims` is
monkeypatched at the `claim_extract` module boundary so the entail gate is deterministic.

What these prove: the CODE gates (predicate-in-active-set, verbatim span-verify, entail
fail-closed), the fail-safe → [] paths, and entity-vs-value object routing. What they do
NOT prove (Rule 3): that a LIVE model chooses the right predicate/object or copies a truly
verbatim quote — that is a held-out live-LLM concern, out of scope for the core.
"""
from __future__ import annotations

import asyncio

import roster_vertical.claim_extract as ce
from roster_vertical.claim_extract import (
    extract_typed_claims,
    extract_typed_claims_counted,
)
from roster_vertical.claim_predicates import SLICE1_PREDICATES

# A YC-style block. Every fixture quote below is a genuine contiguous substring of this text.
BLOCK = (
    "Acme AI is a developer tools company building an AI coding assistant for enterprise "
    "engineering teams. Acme uses retrieval-augmented generation on top of large language "
    "models, and positions itself as faster and more accurate than GitHub Copilot. The "
    "founders previously worked at Stripe."
)
SUBJECT = "Acme AI"


class _FakeJson:
    """Scripted complete_json client (records calls). Payload may be an Exception to raise."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []

    async def complete_json(self, *, model, system, user):
        self.calls.append((model, system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _entail_all(verdict_map=None):
    """Fake entail_claims: returns True for each claim unless its text is in `verdict_map`
    mapped to a non-True verdict. Matches by the object substring for readability."""
    async def fake_entail(*, claims, client=None, model=None, **kw):
        out = []
        for c in claims:
            v = True
            if verdict_map:
                for needle, verdict in verdict_map.items():
                    if needle in c["text"]:
                        v = verdict
                        break
            out.append(v)
        return out
    return fake_entail


def _run(payload, *, monkeypatch, entail=None, predicates=None, client="fake"):
    if entail is None:
        entail = _entail_all()
    monkeypatch.setattr(ce, "entail_claims", entail)
    cl = _FakeJson(payload) if client == "fake" else client
    return asyncio.run(extract_typed_claims(
        block_text=BLOCK, subject_name=SUBJECT,
        predicates=predicates if predicates is not None else SLICE1_PREDICATES,
        client=cl,
    ))


# --------------------------------------------------------------------------- happy path ----- #
def test_happy_path_returns_typed_grounded_claims(monkeypatch):
    payload = {"claims": [
        {"predicate": "targets_customer", "object": "enterprise engineering teams",
         "quote": "for enterprise engineering teams"},
        {"predicate": "uses_technology", "object": "retrieval-augmented generation",
         "quote": "Acme uses retrieval-augmented generation on top of large language models"},
        {"predicate": "compared_to", "object": "GitHub Copilot",
         "quote": "more accurate than GitHub Copilot"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    assert len(out) == 3
    preds = {c["predicate"] for c in out}
    assert preds == {"targets_customer", "uses_technology", "compared_to"}
    # every returned claim carries a verbatim quote and the registry-declared object_kind
    for c in out:
        assert c["quote"] in BLOCK
        assert set(c) == {"predicate", "object_kind", "object_value",
                          "object_entity_name", "quote"}


# ------------------------------------------------- span gate drops a fabricated quote ------- #
def test_span_gate_drops_fabricated_quote(monkeypatch):
    payload = {"claims": [
        {"predicate": "offers_product", "object": "AI coding assistant",
         "quote": "building an AI coding assistant"},                       # real substring
        {"predicate": "offers_product", "object": "a database product",
         "quote": "Acme also ships a managed vector database"},             # NOT in the block
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    assert [c["object_value"] for c in out] == ["AI coding assistant"]      # fabricated one dropped


# --------------------------------------------- predicate gate drops an off-list predicate --- #
def test_predicate_gate_drops_off_list_predicate(monkeypatch):
    payload = {"claims": [
        {"predicate": "raises_funding", "object": "Series A",              # not an active predicate
         "quote": "The founders previously worked at Stripe"},
        {"predicate": "operates_in_category", "object": "developer tools",
         "quote": "Acme AI is a developer tools company"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    assert [c["predicate"] for c in out] == ["operates_in_category"]


def test_predicate_gate_respects_active_status(monkeypatch):
    # A predicate present in the passed list but marked non-active is NOT emittable.
    preds = SLICE1_PREDICATES + [{"name": "internal_note", "status": "candidate",
                                  "object_kind": "value", "description": "not for production"}]
    payload = {"claims": [
        {"predicate": "internal_note", "object": "whatever",
         "quote": "The founders previously worked at Stripe"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch, predicates=preds)
    assert out == []


# --------------------------------------------------- entail gate drops an unsupported claim - #
def test_entail_gate_drops_unsupported_claim(monkeypatch):
    payload = {"claims": [
        {"predicate": "uses_technology", "object": "large language models",
         "quote": "on top of large language models"},                       # supported
        {"predicate": "claims_differentiator", "object": "cheapest on the market",
         "quote": "positions itself as faster and more accurate"},          # judge says False
    ]}
    out = _run(payload, monkeypatch=monkeypatch,
               entail=_entail_all({"cheapest on the market": False}))
    assert [c["object_value"] for c in out] == ["large language models"]


def test_entail_gate_drops_on_none_verdict(monkeypatch):
    # An UNJUDGED claim (None verdict) is fail-closed dropped, never kept.
    payload = {"claims": [
        {"predicate": "uses_technology", "object": "large language models",
         "quote": "on top of large language models"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch,
               entail=_entail_all({"large language models": None}))
    assert out == []


# ---------------------------------------------------------------------------- fail-safe ----- #
def test_no_client_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(ce, "entail_claims", _entail_all())
    out = asyncio.run(extract_typed_claims(
        block_text=BLOCK, subject_name=SUBJECT, predicates=SLICE1_PREDICATES, client=None))
    assert out == []


def test_malformed_json_returns_empty(monkeypatch):
    # complete_json returns a non-dict → nothing parseable → []
    out = _run(["not", "a", "dict"], monkeypatch=monkeypatch)
    assert out == []


def test_extractor_error_returns_empty(monkeypatch):
    # complete_json raises → fail-safe []
    out = _run(RuntimeError("api down"), monkeypatch=monkeypatch)
    assert out == []


def test_empty_block_returns_empty(monkeypatch):
    monkeypatch.setattr(ce, "entail_claims", _entail_all())
    out = asyncio.run(extract_typed_claims(
        block_text="   ", subject_name=SUBJECT, predicates=SLICE1_PREDICATES,
        client=_FakeJson({"claims": []})))
    assert out == []


# ------------------------------------------------ entity-vs-value object routing ------------ #
def test_entity_vs_value_object_routing(monkeypatch):
    payload = {"claims": [
        # entity predicate → name goes to object_entity_name, object_value stays ""
        {"predicate": "compared_to", "object": "GitHub Copilot",
         "quote": "more accurate than GitHub Copilot"},
        {"predicate": "operates_in_category", "object": "developer tools",
         "quote": "Acme AI is a developer tools company"},
        # value predicate → value goes to object_value, object_entity_name stays ""
        {"predicate": "offers_product", "object": "AI coding assistant",
         "quote": "building an AI coding assistant"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    by_pred = {c["predicate"]: c for c in out}

    ent = by_pred["compared_to"]
    assert ent["object_kind"] == "entity"
    assert ent["object_entity_name"] == "GitHub Copilot" and ent["object_value"] == ""

    cat = by_pred["operates_in_category"]
    assert cat["object_kind"] == "entity"
    assert cat["object_entity_name"] == "developer tools" and cat["object_value"] == ""

    val = by_pred["offers_product"]
    assert val["object_kind"] == "value"
    assert val["object_value"] == "AI coding assistant" and val["object_entity_name"] == ""


def test_empty_object_or_quote_dropped(monkeypatch):
    payload = {"claims": [
        {"predicate": "offers_product", "object": "", "quote": "building an AI coding assistant"},
        {"predicate": "offers_product", "object": "AI coding assistant", "quote": ""},
        {"predicate": "offers_product", "object": "AI coding assistant",
         "quote": "building an AI coding assistant"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    assert len(out) == 1 and out[0]["object_value"] == "AI coding assistant"


# ------------------------------------------------ entail-call count (H1 §C cost accounting) - #
def test_counted_reports_one_entail_call_when_survivors(monkeypatch):
    # A block with ≥1 span-verified survivor makes exactly ONE batched entail call.
    payload = {"claims": [
        {"predicate": "uses_technology", "object": "large language models",
         "quote": "on top of large language models"},
        {"predicate": "compared_to", "object": "GitHub Copilot",
         "quote": "more accurate than GitHub Copilot"},
    ]}
    monkeypatch.setattr(ce, "entail_claims", _entail_all())
    out, n_entail = asyncio.run(extract_typed_claims_counted(
        block_text=BLOCK, subject_name=SUBJECT, predicates=SLICE1_PREDICATES,
        client=_FakeJson(payload)))
    assert len(out) == 2
    assert n_entail == 1                      # one batched judge call for the whole block


def test_counted_reports_zero_entail_calls_when_no_survivors(monkeypatch):
    # No span-verified survivor → the judge is never called → zero entail spend.
    payload = {"claims": [
        {"predicate": "offers_product", "object": "a database product",
         "quote": "Acme also ships a managed vector database"},   # NOT in the block → dropped
    ]}
    monkeypatch.setattr(ce, "entail_claims", _entail_all())
    out, n_entail = asyncio.run(extract_typed_claims_counted(
        block_text=BLOCK, subject_name=SUBJECT, predicates=SLICE1_PREDICATES,
        client=_FakeJson(payload)))
    assert out == [] and n_entail == 0


def test_counted_counts_the_entail_call_even_when_all_dropped(monkeypatch):
    # Survivors reach the judge (1 call spent) but all get a False verdict → [] with n_entail=1.
    payload = {"claims": [
        {"predicate": "uses_technology", "object": "large language models",
         "quote": "on top of large language models"},
    ]}
    monkeypatch.setattr(ce, "entail_claims",
                        _entail_all({"large language models": False}))
    out, n_entail = asyncio.run(extract_typed_claims_counted(
        block_text=BLOCK, subject_name=SUBJECT, predicates=SLICE1_PREDICATES,
        client=_FakeJson(payload)))
    assert out == [] and n_entail == 1        # the batched call was made (spent) then dropped


def test_plain_wrapper_unchanged_return_contract(monkeypatch):
    # The plain extract_typed_claims still returns a bare list (back-compat).
    payload = {"claims": [
        {"predicate": "uses_technology", "object": "large language models",
         "quote": "on top of large language models"},
    ]}
    out = _run(payload, monkeypatch=monkeypatch)
    assert isinstance(out, list) and len(out) == 1


def test_prompt_is_rendered_from_passed_predicates(monkeypatch):
    # The system prompt lists the passed predicate names (vertical-generic, not hardcoded).
    cl = _FakeJson({"claims": []})
    monkeypatch.setattr(ce, "entail_claims", _entail_all())
    asyncio.run(extract_typed_claims(
        block_text=BLOCK, subject_name=SUBJECT, predicates=SLICE1_PREDICATES, client=cl))
    _, system, _user = cl.calls[0]
    for p in SLICE1_PREDICATES:
        assert p["name"] in system
    assert SUBJECT in system
