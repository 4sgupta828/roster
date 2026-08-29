"""M&A / corp-dev ANSWER MODE — reframes diligence for a corporate ACQUIRER, not a financial investor.

Supplied via the manifest `answer_modes` map and selected per request (mode="acquirer"). It is a
compose ADDENDUM threaded into the answer directive (the kernel owns the mechanics), so the same
grounded, verbatim-cited evidence is re-lensed around strategic fit / IP complementarity / build-vs-buy
/ integration risk — the questions an acquirer asks, which differ from an investor's return lens.
"""
from __future__ import annotations

MA_DIRECTIVE = """REFRAME THIS ANSWER FOR A CORPORATE ACQUIRER evaluating this company as an
ACQUISITION or PARTNERSHIP target — not a financial investor seeking a return. Keep the same grounded,
verbatim-cited evidence, but organize the findings around the acquirer's decision. Include a section
ONLY when the verified findings support it:

- **Strategic rationale** — what capability, product, market access, or IP this target would add; the
  strategic gap it fills. Strictly from cited facts.
- **Technology & IP complementarity** — how the target's technology/patents complement or overlap the
  acquirer's; unique IP or know-how (granted patents vs. pending); key technical talent embedded in it.
- **Competitive angle** — is this an OFFENSIVE move (enter/lead a category) or a DEFENSIVE one (deny a
  rival, protect a flank)? Name the competitors and the positioning the evidence states.
- **Build-vs-buy** — what the evidence says about the target's lead time, moat, and differentiation that
  an acquirer could NOT easily replicate in-house (or could).
- **Team & talent** — founders, key researchers/engineers, and retention-relevant signals (acqui-hire
  value); concentration of critical people.
- **Integration & execution risk** — customer/revenue concentration, dependencies (single supplier,
  single model provider), regulatory or contractual entanglements, cultural/technical integration flags
  the filings disclose.
- **Open questions for the acquirer** — the highest-value unknowns diligence would need to close next
  (e.g. cap table, private financials, key-person agreements) — framed as an evidence GAP.

DISCIPLINE: report the grounded facts an acquirer needs; do NOT recommend whether to acquire, name a
price, or predict deal success — say plainly where the evidence does not establish those. Keep the
fact-vs-signal separation and the tier discipline of the base format."""
