"""Offline test: precision-lookup end-to-end, scored by the lookup scorer."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.eval.lookup_scoring import score_lookup
from roster_kernel.eval.schema import ExpectedRow, LookupCase
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.precision import CellOut, LookupExtraction, run_precision_lookup
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


class _LLM:
    def __init__(self, extraction): self._e = extraction
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._e, output_tokens=5)


def _source() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="t",
                         text="Entity X reported an amount of 9.8 percent in the report.",
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    return src


def _run(extraction):
    return asyncio.run(run_precision_lookup(
        question="amount for X", llm=_LLM(extraction), embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="t", budget=BudgetState(max_calls=5),
    ))


def test_grounded_cell_scores_full() -> None:
    trace = _run(LookupExtraction(cells=[
        CellOut(row_key={"entity": "X"}, column="amount", value="9.8",
                atom_id="a1", quote="reported an amount of 9.8 percent"),
    ]))
    case = LookupCase(id="amount for X", expected_rows=(
        ExpectedRow(row_key={"entity": "X"}, cells={"amount": "9.8"}),))
    assert score_lookup(case, trace).fully_correct


def test_fabricated_value_is_dropped() -> None:
    # LLM claims 12.3 but the quote (even if real) doesn't contain 12.3 -> dropped
    trace = _run(LookupExtraction(cells=[
        CellOut(row_key={"entity": "X"}, column="amount", value="12.3",
                atom_id="a1", quote="reported an amount of 9.8 percent"),
    ]))
    assert trace.rows == []            # audit gate dropped the cell


def test_ungrounded_quote_is_dropped() -> None:
    trace = _run(LookupExtraction(cells=[
        CellOut(row_key={"entity": "X"}, column="amount", value="9.8",
                atom_id="a1", quote="a quote that is not in the block at all"),
    ]))
    assert trace.rows == []
