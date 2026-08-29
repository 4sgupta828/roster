"""PRIOR_DRAFT_PROMPT — the tech vertical's parametric-draft directive (ROSTER_PARAMETRIC_LED).

Steers the kernel's `draft_prior` call. Domain wording lives HERE; the kernel stays domain-free
(kernel litmus). The model drafts an answer from its OWN integrated knowledge BEFORE retrieval,
separating checkable FACTS (which T2 verifies against the span-gate) from its REASONING.
"""
from __future__ import annotations

PRIOR_DRAFT_PROMPT = """You are drafting an answer from your own knowledge BEFORE any retrieval.

Produce two things:
(a) a brief OUTLINE — the sections/axes a strong answer to this question needs. STRUCTURE ONLY: the \
shape of the answer, the dimensions it must cover. Put NO facts in the outline.
(b) the discrete CLAIMS your answer would make. Tag each claim's `kind`:
    - 'fact' — a checkable assertion: a number, date, name, event, funding amount, attribution, or \
any statement that could be right or wrong.
    - 'reasoning' — your synthesis, interpretation, or judgment (not a single checkable datum).

Separate clearly what you KNOW as fact from what you REASON.

For each FACT claim also give:
    - `needs_freshness`: true if the fact could have changed recently (a current valuation, latest \
release, who currently holds a role, an ongoing round) — false for a stable historical fact.
    - `verify_query`: a targeted search string that would find evidence for THAT specific claim \
(name the specific entity/number/event — not a generic restatement of the question).

Be honest: if you are unsure of a fact, STILL list it as a fact (kind='fact'). Every fact will be \
independently verified against retrieved evidence — an unverifiable fact is labeled, never dropped \
silently and never presented as grounded. Do not inflate reasoning into fact or hide a shaky fact \
as reasoning.

REQUIRED: the `claims` list must be SUBSTANTIAL and carry the actual substance of your answer, a MIX of \
'fact' and 'reasoning'. The OUTLINE is only the shape; the CLAIMS are the content. NEVER return an empty \
`claims` list and NEVER put your reasoning solely in the outline.

SCALE THE BREADTH TO THE QUESTION:
- A reasoning-heavy question (how/why something works) → several 'reasoning' claims (the mechanism, the \
causes, the implications) plus the 'fact' claims that anchor them (aim ~6-15).
- An ENUMERATION / "best-X" / "which tools|companies|repos|models" / "map the landscape" question → be \
COMPREHENSIVE: draft ONE fact-claim PER CANDIDATE ITEM across the WHOLE space, grouped by the outline's \
sections, and DO NOT stop at the obvious few — aim for 20-40 items when the field is broad (a strong \
answer to "the best OSS AI building blocks" names the model/serving/inference/gateway/agent/RAG/vector- \
db/observability/fine-tuning layers, ~25+ specific repos, not 8). For EACH enumerated item, put a \
CONCRETE, CHECKABLE metric IN the claim (GitHub stars, adoption/who-uses-it, release recency) so it can \
be grounded — and set `verify_query` to name that EXACT item plus the metric (e.g. "vLLM github stars \
adoption", "LangGraph stars production use"). Breadth AND per-item grounding are both required; a thin \
list of a few items is a failed draft for an enumeration question."""
