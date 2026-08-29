"""Tech answer-shaping directives (opaque prose the kernel threads into compose).

A diligence-memo structure for an investor audience. Sections appear ONLY when supported by
verified findings — never fabricate a heading to fill it (empty mandatory sections pressure
the model to invent, which attacks the provenance gate from the prompt side). Market signal
is a SEPARATE, explicitly-labeled section so sentiment is never merged into grounded fact.
"""
from __future__ import annotations

TECH_ANSWER_FORMAT = """Write for an investor doing diligence. Markdown. Include a section ONLY \
when the verified findings support it — never add an empty or speculative heading.

Sections, in order, each present only if supported:
- **Bottom line** — the 1–3 sentence read a partner needs first, strictly from verified facts.
- **Funding & traction** — rounds, investors, revenue/loss, customers, hiring/repo momentum \
(from filings/funding records/code), each with a verbatim [n] citation.
- **Technology & IP** — architecture/approach claims, benchmark results (with the exact figure), \
granted vs. pending patents. Distinguish a GRANTED patent from a pending APPLICATION.
- **Competitive landscape** — named competitors/substitutes and any comparison the evidence states.
- **Market signal** — news / forum / social SENTIMENT, clearly framed as perception and momentum, \
NOT fact ("coverage suggests…", "sentiment is…"). Never state a signal as an established number.
- **Not addressed** — what the retrieved evidence does NOT cover (an evidence GAP, not an inference).

Every factual sentence carries an inline [n] tied to a verified quote. Do not rank a single \
"best" investment or predict a winner; report the grounded facts and name what the evidence \
does not establish. Optional inline highlight markers the UI may render: [[F]]…[[/F]] (a hard \
fact/number), [[R]]…[[/R]] (reasoning), [[K]]…[[/K]] (key context)."""

# Sharper A/B variant used only when the diligence-synthesis flag is ON (Rule 20 seam).
# Same section set; tighter in-section discipline. None-safe: falls back to TECH_ANSWER_FORMAT.
TECH_DILIGENCE_SYNTHESIS_FORMAT = TECH_ANSWER_FORMAT + """

Additional discipline when synthesizing:
- Preserve exact figures ($, %, headcount, benchmark score) verbatim; never round or paraphrase a number.
- Treat a filing's forward-looking statements and a press release as INTENT, not realized results.
- Attribute every competitive or market claim to the specific source that states it; do not aggregate \
sentiment into an implied fact. Avoid vague momentum words ("promising", "explosive") unless quoted."""

# Appended to the compose directive when the AUTHORITY-BASIS flag is ON (ROSTER_AUTHORITY_BASIS): the
# FLOOR that keeps an answer's factual basis on the highest-authority sources while sentiment stays a
# labeled signal. Opaque prose threaded into compose exactly like TECH_ANSWER_FORMAT. OFF → not appended.
TECH_AUTHORITY_BASIS_DIRECTIVE = """Ground every FACTUAL sentence in the highest-tier source available. \
Sources tagged opinion / blog / social / unknown-provenance are SUPPLEMENTARY — they may corroborate or \
add color but are NEVER the sole basis for a stated fact; where only such a source supports a point, \
present it as signal ('X argues…', 'coverage suggests…') in the market-signal register, not as an \
established fact."""

# Appended to the compose directive when the answer-visuals flag is ON: push toward comparison
# tables / ranked options built STRICTLY from verified findings (never fabricated structure).
TECH_VISUAL_GUIDANCE = """When the verified findings support it, use a COMPARISON TABLE or a RANKED \
list to make a multi-entity comparison scannable (e.g. companies × revenue/growth/margin, or \
approaches × benchmark/tradeoff). Build the table ONLY from figures/claims already cited in the \
answer — every cell must trace to a verified quote; never invent a row, column, or value to fill the \
grid. If the findings don't support a clean comparison, write prose instead — a fabricated table is worse \
than none."""

# Appended when the answer-charts flag is ON: let compose populate ONE grounded bar chart (validated
# in code). Numbers must be verbatim from cited evidence.
TECH_CHART_GUIDANCE = """If the answer contains a set of COMPARABLE NUMERIC figures already cited (e.g. \
revenue by fiscal year, funding by round, a benchmark across models, headcount across companies), you may \
emit ONE simple bar chart of those exact values. Every bar's value and label must come verbatim from a \
cited finding — never chart a number the evidence didn't state, never estimate, never mix units."""

# Appended when the reasoning-read flag is ON: a typed interpretation layer + a confidence read,
# both validated in code (no new facts).
TECH_REASONING_FORMAT = """After the answer, add a short INTERPRETATION layer that adds no new facts — \
each item strictly grounded in what the answer already established:
- Tensions / gaps: where the evidence conflicts or is thin (e.g. strong revenue growth but disclosed \
customer concentration; benchmark claim not third-party-verified).
- What would change the read: the single piece of evidence that would most move the diligence view.
- Confidence: a brief, honest read of how far to trust this answer, decomposed by evidence tier — \
higher when it rests on audited filings, lower when it leans on preprints or news signal. \
Never introduce a fact the answer did not already state."""
