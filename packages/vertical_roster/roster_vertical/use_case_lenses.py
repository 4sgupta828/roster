"""USE-CASE LENSES — per-question modes that re-lens the SAME grounded evidence for what the user is
actually trying to do. Selected via the request `mode` (like the `acquirer` M&A mode); each is a dict
`{directive, suppress_authority}` the app threads into compose + ranking.

The deep-tech intelligence product serves several jobs, and each wants a different evidence MIX and
posture — not a different corpus:
  - wisdom / foresight lead with EXPERT OPINION + human DISCUSSION and neutralize the authority
    tier-boost (`suppress_authority=True`) so a named expert's essay or a practitioner thread isn't
    demoted below a filing on a "what do experts think / what's coming" question;
  - genesis / market / whitespace / moat keep the authority ordering (filings/patents/grants/papers
    lead) and just steer what the answer foregrounds.

Grounding is UNCHANGED — every claim still needs a span-verified quote, and expert/opinion sources are
labeled as opinion, never presented as established fact (authority.py keeps them non-controlling).
"""
from __future__ import annotations

USE_CASE_LENSES: dict[str, dict] = {
    # 5. Expert opinions on tech trends — what credible people think is COMING and why.
    "foresight": {
        "suppress_authority": True,
        "directive": (
            "USE-CASE = EXPERT FORESIGHT. Answer what credible, NAMED experts think is coming and WHY. "
            "Lead with expert analysis/essays/newsletters and recorded expert discussion — attribute "
            "each view to its author and date it. Separate emerging consensus from contrarian takes, and "
            "surface the REASONING, not just the prediction. Label all of it as informed opinion/"
            "foresight (not established fact); use filings/papers/benchmarks as supporting context for "
            "what the forecasts rest on."),
    },
    # 6. Actual human discussions that bring out wisdom — practitioner reasoning + disagreement.
    "wisdom": {
        "suppress_authority": True,
        "directive": (
            "USE-CASE = PRACTITIONER WISDOM. Lead with what expert practitioners and the community "
            "actually argue — expert essays, recorded discussions, and high-signal forum/Q&A threads. "
            "Surface the real reasoning, hard-won tradeoffs, and DISAGREEMENTS, not just tidy "
            "conclusions. Attribute and label these as informed opinion/experience; primary filings and "
            "papers are context here, not the lead. Where the community is divided, say so and give the "
            "sides."),
    },
    # 7. Tech evolution / genesis / ecosystem history — how it came to be.
    "genesis": {
        "suppress_authority": False,
        "directive": (
            "USE-CASE = GENESIS / EVOLUTION. Tell how the technology and its key players emerged over "
            "time. Lead with encyclopedic/reference and seminal sources; give a chronological arc "
            "(origins → milestones → current state) with each step attributed and, where possible, "
            "dated. Name the ecosystem players and how they relate. Distinguish established history from "
            "contested/uncertain origins."),
    },
    # 1. Market research — sizing, structure, dynamics, ecosystem players.
    "market": {
        "suppress_authority": False,
        "directive": (
            "USE-CASE = MARKET RESEARCH. Map the market and ecosystem: the players, funding, structure, "
            "and dynamics. Lead with filings, grant records, funding data, and company records; quantify "
            "with EXACT cited figures (revenue, raises, headcount, grant amounts). Name the segments and "
            "who occupies each; note concentration vs fragmentation. Flag where the evidence is thin "
            "rather than estimating."),
    },
    # 2/3. Startup-creation opportunity + whitespace identification.
    "whitespace": {
        "suppress_authority": False,
        "directive": (
            "USE-CASE = WHITESPACE / OPPORTUNITY. Identify unserved problem/tech space. Cross-reference "
            "where RESEARCH and GRANTS show active investigation but few companies/products exist yet, "
            "and where practitioner discussion voices an unmet need. Name each candidate gap EXPLICITLY "
            "and cite the evidence for it (the activity that exists AND the absence). Distinguish a real, "
            "evidenced whitespace from merely thin coverage — say which it is."),
    },
    # 4. Evaluate existing startups' moats & differentiation.
    "moat": {
        "suppress_authority": False,
        "directive": (
            "USE-CASE = MOAT & DIFFERENTIATION. Evaluate what is actually defensible: patents/IP, "
            "proprietary technology, reproducible benchmark leads, ecosystem lock-in, and traction "
            "(repo/model adoption, customers). Lead with patents, benchmarks, filings, and traction "
            "data; for each claimed advantage say whether the evidence shows a DURABLE moat or a "
            "TEMPORARY lead, and name what would erode it. Treat vendor 'differentiation' claims as "
            "claims unless independently evidenced."),
    },
}
