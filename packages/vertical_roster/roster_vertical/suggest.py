"""Suggested follow-up questions directive (opaque prose the kernel threads)."""
from __future__ import annotations

import os

# The original ("minor extension") suggester — kept as the flag-OFF default (byte-identical).
TECH_SUGGEST_PROMPT = """Given a tech-diligence Q&A, propose 3–4 self-contained follow-up questions \
that deepen the diligence, across four angles:
- deeper understanding (a technology/financial detail worth pulling next),
- adjacent discovery (a competitor, comparable, or upstream/downstream player to examine),
- risk & tradeoffs (a concentration, dependency, IP, or execution risk to probe),
- toward a decision (what evidence would most change the investment view).
Each must be concrete, answerable from filings/patents/papers/code/news, and NOT an investment \
recommendation. Return them as short standalone questions."""


# The STRATEGIC suggester (ROSTER_STRATEGIC_NEXT): the CENTRAL questions a serious decision-maker
# would ask next — the people who RUN, FUND, or BUILD the business — not minor extensions of the
# answer. Each question is tagged with its lens so the UI can show the three perspectives.
TECH_SUGGEST_PROMPT_STRATEGIC = """You suggest the CENTRAL next questions a serious decision-maker \
would ask after this tech Q&A — the questions that actually determine how you'd run, fund, or build \
this business. NOT minor extensions or trivia deepeners. Channel three personas and give the \
sharpest question each would ask about THIS specific company / technology / market, grounded in \
what the answer surfaced:

- CEO — runs the business: competition & moat, strategy & positioning, customers & demand, pricing \
  & unit economics, go-to-market, revenue durability, the existential threat.
- VC — funds the business: market size & timing, the core bet and what breaks it, path to a big \
  outcome, comparable exits, the tech-trend tailwind or headwind, capital efficiency.
- CTO — builds it through the science & engineering: the technical approach & what truly \
  differentiates it, scalability & reliability, the research/engineering frontier and how far it can \
  go, key technical risks & dependencies.

Return 4–6 questions total spanning AT LEAST TWO of the three lenses, and set each question's `tag` \
to exactly one of: "CEO", "VC", "CTO". Each question must be:
- a single, self-contained question that stands alone if clicked;
- CENTRAL and consequential — it would change how you run, fund, or build the business (never a \
  trivia deepener like "how does X's async layer handle a network split?");
- concrete to the actual company / technology / market in the answer (NAME it), and answerable from \
  filings / papers / patents / code / news / market data;
- NOT an investment recommendation, and NOT a restatement of what the answer already covered.
Prefer the questions whose answers would most change a real decision."""


def strategic_next_on() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_STRATEGIC_NEXT swaps the follow-up suggester for the
    CEO/VC/CTO strategic set (lens-tagged) AND drops the duplicate in-answer 'What to explore next'
    list, so next-questions live in ONE place. Read at manifest build → flip = redeploy. OFF →
    byte-identical (original suggester + the in-answer questions)."""
    return os.environ.get("ROSTER_STRATEGIC_NEXT", "").lower() in ("1", "true", "yes")


def tech_suggest_prompt() -> str:
    """The active suggester directive — strategic when the flag is on, else the original."""
    return TECH_SUGGEST_PROMPT_STRATEGIC if strategic_next_on() else TECH_SUGGEST_PROMPT
