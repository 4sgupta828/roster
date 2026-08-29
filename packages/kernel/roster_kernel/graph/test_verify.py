"""C-6 verification pipeline — offline contract tests (scripted LLM)."""
from __future__ import annotations

import asyncio

from roster_kernel.graph.verify import EdgeVerdict, verify_edge_candidate


class ScriptedLLM:
    def __init__(self, verdict):
        self._v = verdict

    async def complete(self, *, system, messages, response_format, max_tokens=300,
                       temperature=None):
        class R:
            parsed = self._v
        return R()


_BLOCKS = [("d1", "b1", "Primary aldosteronism is a common and underdiagnosed cause of "
                        "resistant hypertension, and screening with an aldosterone-renin "
                        "ratio is recommended.")]
_SENT = "primary aldosteronism underlies_presentation_of hypertension (resistant)"


def _run(v, blocks=_BLOCKS):
    return asyncio.run(verify_edge_candidate(sentence=_SENT, blocks=blocks,
                                             llm=ScriptedLLM(v)))


def test_verbatim_quote_passes_and_carries_provenance():
    r = _run(EdgeVerdict(verdict="supported", notable=True, block_index=0,
                         quote="underdiagnosed cause of resistant hypertension"))
    assert r["supported"] and r["notable"] and r["document_id"] == "d1"


def test_paraphrased_quote_is_rejected_by_the_span_gate():
    r = _run(EdgeVerdict(verdict="supported", notable=True, block_index=0,
                         quote="PA frequently causes refractory high blood pressure"))
    assert not r["supported"] and r["reason"] == "quote_not_verbatim"


def test_quote_lacking_subject_is_vetoed_structurally():
    r = asyncio.run(verify_edge_candidate(
        sentence=_SENT, blocks=_BLOCKS, subject="autoimmune hepatitis",
        llm=ScriptedLLM(EdgeVerdict(verdict="supported", notable=True, block_index=0,
                                    quote="underdiagnosed cause of resistant hypertension"))))
    assert not r["supported"] and r["reason"] == "quote_lacks_subject"
    r2 = asyncio.run(verify_edge_candidate(
        sentence=_SENT, blocks=_BLOCKS, subject="primary aldosteronism",
        llm=ScriptedLLM(EdgeVerdict(verdict="supported", notable=True, block_index=0,
                                    quote="Primary aldosteronism is a common and "
                                          "underdiagnosed cause of resistant hypertension"))))
    assert r2["supported"]


def test_unsupported_bad_index_and_no_blocks_fail_safe():
    assert not _run(EdgeVerdict(verdict="unsupported"))["supported"]
    assert not _run(EdgeVerdict(verdict="supported", quote="x", block_index=7))["supported"]
    assert _run(EdgeVerdict(verdict="supported"), blocks=[])["reason"] == "no_blocks"
