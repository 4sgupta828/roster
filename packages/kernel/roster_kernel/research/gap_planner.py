"""Gap-fill planner — turn an under-evidenced question into a concrete corpus-ingest plan.

When the corpus can't fully answer a question, the LLM proposes what to ADD so it could: a
set of ingest JOBS mapped onto the vertical's available connectors ({connector, query, limit})
plus gold-standard sources to RECOMMEND that no connector can yet fetch. This is a semantic
judgement (what evidence would answer this?) so it is the LLM's job end to end (Rule 18); the
kernel owns only the mechanics and hard-validates the connector keys the model returns.

The domain knowledge — what each connector fetches, what "high-quality evidence" means here,
which gold sources to name — lives entirely in the vertical's `gap_prompt`. The kernel passes
the available connector keys and never names a domain source itself.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from roster_kernel.providers.llm import LLMClient


class GapFillJob(BaseModel):
    connector: str = Field(description="one of the AVAILABLE connector keys given in the prompt")
    query: str = Field(description="the search query to run against that connector")
    limit: int = Field(default=200, description="how many items to fetch (the planner's suggestion)")
    kind: str = Field(default="", description="short label for what this fetches, e.g. 'RCTs', 'literature', 'drug label'")
    rationale: str = Field(default="", description="one line: why this fills the gap")
    quality: str = Field(default="", description="evidence quality, e.g. 'high — phase 3 RCTs'")


class GapRecommendation(BaseModel):
    """A gold-standard source we should add but no connector can fetch yet (informational)."""
    source: str = Field(description="the source/publisher, e.g. 'Cochrane systematic reviews'")
    what: str = Field(default="", description="what to look for there for this question")
    why: str = Field(default="", description="why it is high-quality evidence for this gap")


class GapFillPlan(BaseModel):
    summary: str = Field(default="", description="one sentence naming what evidence is missing")
    jobs: list[GapFillJob] = Field(default_factory=list, description="actionable ingest jobs (queueable)")
    recommendations: list[GapRecommendation] = Field(
        default_factory=list, description="gold sources to add later (not yet fetchable)")


async def plan_gap_fill(
    *, llm: LLMClient, gap_prompt: str, question: str, answer: str,
    coverage_gaps: list[str], available_connectors: list[str], max_tokens: int = 1800,
) -> GapFillPlan:
    """Ask the LLM which sources would let the corpus answer `question`.

    Returns a plan whose `jobs` reference ONLY connectors in `available_connectors` (any others
    are dropped here — code owns the structural guarantee, the model owns the judgement).
    """
    avail = [c for c in available_connectors if c]
    gaps = "\n".join(f"- {g}" for g in (coverage_gaps or [])) or "- (none stated; the answer was thin or ungrounded)"
    have = (answer or "").strip() or "(the corpus produced no grounded answer)"
    user = (
        f"QUESTION THE CORPUS COULD NOT FULLY ANSWER:\n{question}\n\n"
        f"WHAT THE CORPUS PRODUCED (may be partial/empty):\n{have}\n\n"
        f"STATED COVERAGE GAPS:\n{gaps}\n\n"
        f"AVAILABLE CONNECTOR KEYS (a job's `connector` MUST be exactly one of these): {avail}\n\n"
        "Propose the ingest JOBS that would let us answer this, and any gold-standard sources to "
        "RECOMMEND that no connector above can fetch. Be specific and concrete in every query."
    )
    res = await llm.complete(
        system=gap_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=GapFillPlan, max_tokens=max_tokens)
    plan: GapFillPlan = res.parsed
    allowed = set(avail)
    # Hard structural filter (Rule 18): never trust the model to stay inside the connector set.
    plan.jobs = [j for j in plan.jobs if j.connector in allowed and (j.query or "").strip()]
    for j in plan.jobs:
        j.limit = max(1, min(int(j.limit or 200), 400))   # bound cost/time per job
    return plan
