"""Key-term explainer mechanics: extraction cleaning, dedup, related-edge tidying,
single-term lookup. The LLM is scripted — these test the kernel's code contract (Rule 8),
not model behavior (that's the prod-verify + glossary growth itself)."""
import asyncio

import pytest

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.terms import (
    TermExplanation, TermExplanations, explain_key_terms, explain_single_term, normalize_term)

PROMPT = "explain medical terms"


class ScriptedLLM:
    def __init__(self, parsed):
        self.parsed = parsed
        self.prompts = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.prompts.append((system, messages[0]["content"]))
        return LLMResult(parsed=self.parsed, output_tokens=5, model="scripted")


def test_related_coerces_stringified_list():
    # the model sometimes returns `related` as a CSV string instead of a list — must not 500
    t = TermExplanation(term="TENS", plain="nerve stim",
                        related="acupuncture, gate-control theory of pain; endogenous opioids")
    assert t.related == ["acupuncture", "gate-control theory of pain", "endogenous opioids"]
    assert TermExplanation(term="x", plain="y", related=None).related == []
    assert TermExplanation(term="x", plain="y", related=["a", "b"]).related == ["a", "b"]


def test_normalize_term_collapses_case_and_whitespace():
    assert normalize_term("  eGFR   Value ") == "egfr value"
    assert normalize_term("") == ""


def test_clean_strips_stray_markup_tags():
    # the model sometimes wraps each value in an XML-ish tag (<r>metformin</r>) — must not leak to UI
    llm = ScriptedLLM(TermExplanations(terms=[
        TermExplanation(term="<t>Pioglitazone</t>", category="drug", plain="A TZD antidiabetic.",
                        related=["<r>Rosiglitazone</r>", "<r>PPAR-gamma</r>", "Insulin resistance"]),
    ]))
    out = asyncio.run(explain_key_terms(
        llm=llm, terms_prompt=PROMPT, question="q", answer="Pioglitazone lowers glucose"))
    assert out[0].term == "Pioglitazone"
    assert out[0].related == ["Rosiglitazone", "PPAR-gamma", "Insulin resistance"]


def test_explain_key_terms_dedups_and_cleans():
    llm = ScriptedLLM(TermExplanations(terms=[
        TermExplanation(term="eGFR", category="measure", plain="Kidney filtration estimate.",
                        related=["creatinine", "  eGFR  ", "CKD", "creatinine"]),
        TermExplanation(term="  egfr ", plain="duplicate should drop"),
        TermExplanation(term="metformin", plain=""),          # no definition → dropped
        TermExplanation(term="", plain="nameless → dropped"),
        TermExplanation(term="CKD", category="condition", plain="Chronic kidney disease.",
                        related=["eGFR", "dialysis"]),
    ]))
    out = asyncio.run(explain_key_terms(
        llm=llm, terms_prompt=PROMPT, question="q", answer="eGFR and CKD and metformin"))
    assert [t.term for t in out] == ["eGFR", "CKD"]
    # related: self-reference and duplicates removed, order kept
    assert out[0].related == ["creatinine", "CKD"]
    assert out[1].related == ["eGFR", "dialysis"]
    assert llm.prompts[0][0] == PROMPT


def test_explain_key_terms_empty_answer_skips_llm():
    llm = ScriptedLLM(TermExplanations(terms=[]))
    out = asyncio.run(explain_key_terms(llm=llm, terms_prompt=PROMPT, question="q", answer="  "))
    assert out == [] and llm.prompts == []


def test_explain_single_term_returns_cleaned_entry():
    llm = ScriptedLLM(TermExplanation(
        term="Creatinine", category="measure", plain="A muscle waste product measured in blood.",
        related=["eGFR", "creatinine", "BUN"]))
    got = asyncio.run(explain_single_term(
        llm=llm, terms_prompt=PROMPT, term="creatinine", context="eGFR"))
    assert got is not None and got.term == "Creatinine"
    assert got.related == ["eGFR", "BUN"]          # self-edge dropped
    assert "eGFR" in llm.prompts[0][1]             # linked-from context reached the model


def test_explain_single_term_blank_or_useless():
    llm = ScriptedLLM(TermExplanation(term="x", plain=""))
    assert asyncio.run(explain_single_term(llm=llm, terms_prompt=PROMPT, term="  ")) is None
    assert asyncio.run(explain_single_term(llm=llm, terms_prompt=PROMPT, term="x")) is None
