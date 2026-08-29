"""Offline tests for the lookup scorer + conformance runner.

Fixtures are deliberately DOMAIN-NEUTRAL (generic column/entity names) — the
kernel must not carry any vertical's vocabulary, tests included.
"""
from __future__ import annotations

from roster_kernel.contract.dto import BlockHit, Capability, DocumentRef, EntityRef, Locator, RetrievalRequest
from roster_kernel.contract.manifest import VerticalManifest
from roster_kernel.conformance.runner import run_conformance
from roster_kernel.eval.lookup_scoring import score_lookup
from roster_kernel.eval.schema import EmittedRow, ExpectedRow, LookupCase, LookupTrace


def _case() -> LookupCase:
    return LookupCase(
        id="two_entities",
        category="C1",
        expected_rows=(
            ExpectedRow(row_key={"entity": "x"}, cells={"amount": "9.8%", "size": "$1,200"}),
            ExpectedRow(row_key={"entity": "y"}, cells={"amount": "10.10%"}),
        ),
    )


def test_loose_numeric_match_and_full_credit() -> None:
    trace = LookupTrace(case_id="two_entities", rows=[
        EmittedRow(row_key={"entity": "x"}, cells={"amount": "9.80", "size": "1200"}),
        EmittedRow(row_key={"entity": "y"}, cells={"amount": "10.1"}),
    ])
    s = score_lookup(_case(), trace)
    assert s.matched_rows == 2
    assert s.cell_accuracy == 1.0
    assert s.fully_correct


def test_wrong_adjacent_row_value_loses_credit() -> None:
    # Adversarial trap: y's value placed on x's row.
    trace = LookupTrace(case_id="two_entities", rows=[
        EmittedRow(row_key={"entity": "x"}, cells={"amount": "10.10%", "size": "$1,200"}),
        EmittedRow(row_key={"entity": "y"}, cells={"amount": "10.10%"}),
    ])
    s = score_lookup(_case(), trace)
    assert not s.fully_correct
    assert s.cell_accuracy < 1.0


def test_strict_column_rejects_rounding() -> None:
    case = LookupCase(
        id="strict",
        expected_rows=(ExpectedRow(row_key={"entity": "x"}, cells={"amt": "189.80"}),),
        strict_columns=frozenset({"amt"}),
    )
    trace = LookupTrace(case_id="strict", rows=[EmittedRow(row_key={"entity": "x"}, cells={"amt": "189.8"})])
    assert not score_lookup(case, trace).fully_correct


def test_missing_row_no_credit() -> None:
    trace = LookupTrace(case_id="two_entities", rows=[
        EmittedRow(row_key={"entity": "x"}, cells={"amount": "9.8%", "size": "$1,200"}),
    ])
    s = score_lookup(_case(), trace)
    assert s.matched_rows == 1 and not s.fully_correct


# ---- conformance: domain-neutral fakes that satisfy the Protocols ----------

class _FakeStrategy:
    egress_class = "datacenter"
    engine = "http"
    proxy_enabled = False
    async def fetch(self, url, **opts):  # noqa: ANN001
        return b""


class _FakeConnector:
    def __init__(self):
        self.key = "src"
        self.fetch_strategy = _FakeStrategy()
    async def discover_entities(self, window):  # noqa: ANN001
        return [EntityRef(source_key="src", native_id="1")]
    async def list_documents(self, entity):  # noqa: ANN001
        return [DocumentRef(source_key="src", native_id="d1")]
    async def fetch_artifact(self, doc):  # noqa: ANN001
        return b""


class _FakeRetrieval:
    def __init__(self):
        self.key = "src"
    def capabilities(self):
        return frozenset({Capability.RETRIEVAL})
    def covers(self):
        return {}
    def make_block_loader(self, tenant_id, workspace_id=None):  # noqa: ANN001
        return lambda d, b: None
    async def search(self, req: RetrievalRequest):  # noqa: ANN001
        return [BlockHit(document_id="d1", block_id="b1", text="t", locator=Locator("block_span", "d1"))]


class _FakeGating:
    def gate_applies(self, question, plan):  # noqa: ANN001
        return False
    def claim_in_scope(self, claim, cited_hits):  # noqa: ANN001
        return True
    def coverage_gap(self, question, hits):  # noqa: ANN001
        return None


class _FakeCitation:
    supported_kinds = ("block_span",)
    def verify(self, quote, locator):  # noqa: ANN001
        return True


class _FakePersona:
    def system_prompt(self):
        return "neutral"
    def tool_descriptions(self):
        return {}


class _FakeUI:
    def navigation(self):
        return []
    def search_facets(self):
        return []
    def entity_views(self):
        return []
    def citation_renderers(self):
        return {}
    def deliverable_renderers(self):
        return {}


def test_conformance_partial_manifest_ok_at_p0_fails_at_p3() -> None:
    m = VerticalManifest(name="demo")
    assert run_conformance(m, phase="P0").ok
    rep3 = run_conformance(m, phase="P3")
    assert not rep3.ok
    failed = {r.name for r in rep3.results if not r.passed and not r.skipped}
    assert "entity-taxonomy-declared" in failed
    assert "connectors-conform" in failed


def test_conformance_full_manifest_passes_p4() -> None:
    m = VerticalManifest(
        name="demo",
        entity_types=("thing",),
        scope_dimensions=("region",),
        connectors={"src": _FakeConnector()},
        retrieval_sources={"src": _FakeRetrieval()},
        gating_policy=_FakeGating(),
        citation_verifier=_FakeCitation(),
        persona=_FakePersona(),
        authority_policy=object(),
        eval_gold={"lookup": ["case1"]},
        ui=_FakeUI(),
    )
    rep = run_conformance(m, phase="P4")
    assert rep.ok, rep.summary()


def test_conformance_rejects_misshaped_connector() -> None:
    class _NotAConnector:
        pass
    m = VerticalManifest(name="demo", entity_types=("thing",), connectors={"src": _NotAConnector()})
    rep = run_conformance(m, phase="P1")
    failed = {r.name for r in rep.results if not r.passed and not r.skipped}
    assert "connectors-conform" in failed
