"""Key-term explanation — explain the specialist vocabulary an answer used.

After an answer, the user can ask "explain the key terms": one LLM call extracts the
domain-specific terms that actually appear in the answer and explains each one — what it IS
(plain definition), its PURPOSE (why it matters in this domain), its APPLICATION (how it is
used in practice / in this answer's context), and its RELATED terms (the genuinely connected
concepts around it). The related terms are the EDGES of the domain's vocabulary web: the app
layer accumulates terms + edges into a per-vertical glossary store, and navigating to a
related term that hasn't been explained yet triggers `explain_single_term` — so browsing the
web deepens it. The domain framing (what counts as a "key term", the register of the
explanations, what "related" means) lives entirely in the vertical's `terms_prompt`; the
kernel owns only the mechanics.

Explanations are DEFINITIONAL, not clinical: the prompt forbids new claims about the user's
specific case.
"""
from __future__ import annotations

import re as _re

from pydantic import BaseModel, Field, field_validator

from roster_kernel.providers.llm import LLMClient


class TermExplanation(BaseModel):
    term: str = Field(description="the term exactly as it appears in the answer (display casing)")
    category: str = Field(default="", description="one-word kind, e.g. drug/condition/test/measure/procedure/mechanism")
    plain: str = Field(default="", description="what it IS — one plain-language sentence")
    purpose: str = Field(default="", description="why it matters / what it does or measures in this domain")
    application: str = Field(default="", description="how it is used in practice, incl. how this answer used it")
    related: list[str] = Field(default_factory=list,
                               description="3-6 genuinely connected terms in this domain's knowledge web")

    @field_validator("related", mode="before")
    @classmethod
    def _coerce_related(cls, v):
        # tolerate the model returning `related` as a comma/semicolon/newline-separated STRING instead
        # of a list (a common structured-output quirk the JSON-string recovery doesn't catch).
        if isinstance(v, str):
            return [s.strip() for s in _re.split(r"[,;\n]", v) if s.strip()]
        if v is None:
            return []
        return v


class TermExplanations(BaseModel):
    terms: list[TermExplanation] = Field(default_factory=list)


_TAG_RE = _re.compile(r"</?[A-Za-z][^>]*>")


def strip_markup(s: str) -> str:
    """Remove stray XML/HTML-ish tags the model sometimes wraps values in (e.g. `<r>metformin</r>`)
    and collapse whitespace. Purely structural cleaning of markup — not a semantic decision (Rule 18)."""
    return " ".join(_TAG_RE.sub("", s or "").split())


def normalize_term(term: str) -> str:
    """Stable dedup key: lowercase, collapsed whitespace, tags stripped. Structural, not semantic (Rule 18)."""
    return strip_markup(term).lower()


def _clean(items: list[TermExplanation], max_terms: int) -> list[TermExplanation]:
    """Dedup by normalized term, drop empty/definition-less entries, tidy related lists."""
    seen: set[str] = set()
    out: list[TermExplanation] = []
    for t in items:
        key = normalize_term(t.term)
        if not key or key in seen or not (t.plain or "").strip():
            continue
        seen.add(key)
        t.term = strip_markup(t.term)
        rseen: set[str] = {key}
        rel: list[str] = []
        for r in (t.related or []):
            rk = normalize_term(r)
            if rk and rk not in rseen:
                rseen.add(rk)
                rel.append(strip_markup(r))
        t.related = rel[:6]
        out.append(t)
    return out[:max_terms]


async def explain_key_terms(
    *, llm: LLMClient, terms_prompt: str, question: str, answer: str,
    max_terms: int = 12, max_tokens: int = 4000,
) -> list[TermExplanation]:
    """Extract + explain the key domain terms of an answer. [] when nothing qualifies."""
    clean = (answer or "").strip()
    if not clean:
        return []
    user = (
        f"QUESTION ASKED:\n{question}\n\n"
        f"ANSWER GIVEN:\n{clean}\n\n"
        f"Extract the key specialist terms from the ANSWER above (at most {max_terms}, "
        "most load-bearing first) and explain each. Only terms that actually appear in the "
        "answer; definitions must be consistent with how the answer used them. For each term "
        "also list its related terms — the genuinely connected concepts a reader would want "
        "to navigate to next."
    )
    res = await llm.complete(
        system=terms_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=TermExplanations, max_tokens=max_tokens)
    return _clean(res.parsed.terms or [], max_terms)


async def explain_single_term(
    *, llm: LLMClient, terms_prompt: str, term: str, context: str = "",
    max_tokens: int = 1200,
) -> TermExplanation | None:
    """Explain ONE term on demand (glossary navigation to a not-yet-explained related term).
    Same directive, no answer to anchor to — purely definitional. None when nothing usable."""
    t = (term or "").strip()
    if not t:
        return None
    user = (
        f"TERM TO EXPLAIN:\n{t}\n\n"
        + (f"IT WAS LINKED FROM:\n{context.strip()}\n\n" if context.strip() else "")
        + "Explain this single term: what it is, its purpose in this domain, how it is "
        "applied in practice, and its related terms for further navigation."
    )
    res = await llm.complete(
        system=terms_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=TermExplanation, max_tokens=max_tokens)
    got = _clean([res.parsed], 1)
    return got[0] if got else None
