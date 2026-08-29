"""Precision-lookup capability — domain-free.

Fills specific (row_key × column) cells from the corpus with a provenance +
audit gate: the LLM must return, per cell, a value + a cited atom + a verbatim
quote, and a cell survives only if (a) the quote exists at the atom's locator
(span-check) AND (b) the emitted value actually appears in that quote. This is
the "audit integrity" the lookup eval scores — provenance, not correctness.
"""
from __future__ import annotations

from pydantic import BaseModel

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.eval.schema import EmittedRow, LookupTrace
from roster_kernel.providers.embeddings import Embedder
from roster_kernel.providers.llm import LLMClient
from roster_kernel.research.atoms import AtomStore
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.provenance import BlockSpanVerifier, normalize


class CellOut(BaseModel):
    row_key: dict[str, str]
    column: str
    value: str
    atom_id: str
    quote: str


class LookupExtraction(BaseModel):
    cells: list[CellOut] = []


def _rk(d: dict[str, str]) -> tuple:
    return tuple(sorted((k, str(v).strip().lower()) for k, v in d.items()))


async def run_precision_lookup(
    *,
    question: str,
    llm: LLMClient,
    embedder: Embedder,
    source: RetrievalSource,
    tenant_id: str,
    workspace_id: str | None = None,
    budget: BudgetState,
    system_prompt: str = "Extract requested cell values; cite a verbatim quote per cell.",
    k: int = 20,
) -> LookupTrace:
    """One retrieve → extract → verify pass. Returns a scoreable LookupTrace
    (only audit-passing cells are emitted)."""
    trace = LookupTrace(case_id=question, plan_emitted=True, failed=False)

    budget.reserve()
    qv = embedder.embed([question])[0]
    hits = await source.search(RetrievalRequest(
        query=question, tenant_id=tenant_id, workspace_id=workspace_id,
        query_embedding=list(qv), k=k,
    ))
    atoms = AtomStore()
    atoms.add_hits(hits)

    llm_res = await llm.complete(
        system=system_prompt,
        messages=[{"role": "user", "content": question},
                  {"role": "system", "content": "EVIDENCE:\n" +
                   "\n".join(f"{a.atom_id}: {a.text}" for a in atoms.all())}],
        response_format=LookupExtraction,
    )
    budget.charge(calls=1, tokens=llm_res.output_tokens)

    verifier = BlockSpanVerifier(source.make_block_loader(tenant_id, workspace_id))
    rows: dict[tuple, EmittedRow] = {}
    for cell in llm_res.parsed.cells:
        atom = atoms.get(cell.atom_id)
        if atom is None or atom.locator is None:
            continue                                   # unknown atom → drop
        if not verifier.verify(cell.quote, atom.locator):
            continue                                   # quote not grounded → drop
        if normalize(cell.value) not in normalize(cell.quote):
            continue                                   # value not in its own quote → drop
        key = _rk(cell.row_key)
        row = rows.setdefault(key, EmittedRow(row_key=dict(cell.row_key), cells={}))
        row.cells[cell.column] = cell.value
    trace.rows = list(rows.values())
    return trace
