"""Tech ANSWER-CONTRACT — the question-driven evidence REGIME (flag ROSTER_ANSWER_CONTRACT).

ONE small LLM classification (`TECH_CONTRACT_PROMPT`) decides what KIND of evidence a question demands;
`ANSWER_PROFILES[stance]` then supplies the opaque knobs the kernel threads into retrieval + ranking +
compose. This is where the intelligence lives — retrieval stays generic. The split honours our rules:
the LLM owns the semantic judgment (which regime), code owns the mechanics (stance → knobs), and no
regime EVER fabricates an evidence tier — "current" merely chooses to LEAD with recency and label
un-benchmarked releases as such; "established" leads with authority. Fail-safe: any classification
miss → "balanced" (today's behavior). The kernel interprets none of these strings.
"""
from __future__ import annotations

from .freshness import TECH_FRESHNESS_POLICY

# System prompt for the ONE derivation call. Emits {mode, stance, axes}. mode stays "exploratory"
# (we use the contract for STANCE, not enumeration); stance is the evidence regime.
TECH_CONTRACT_PROMPT = """You classify a TECH-RESEARCH question to decide what KIND of evidence best
answers it. Output JSON with:
- `mode`: always "exploratory".
- `stance`: ONE of:
  - "current"  — the question is about the CURRENT/LATEST state, newest releases, what JUST happened,
    who leads RIGHT NOW, recent news/funding/launches, or where things are HEADED. Freshness matters
    more than long-established proof. (e.g. "what are the latest frontier models", "who leads now",
    "recent AI funding", "what's new in X").
  - "established" — the question is about PROVEN, benchmarked, peer-reviewed, well-tested, foundational
    knowledge: how something WORKS, why, established comparisons, seminal methods, durable technical
    fact. Authority and rigor matter more than recency. (e.g. "how does RAG work", "what is the
    transformer architecture", "proven techniques for X", "benchmarked comparison of established Y").
  - "balanced" — anything that is neither clearly current-events nor clearly foundational, or a mix.
- `axes`: 0-5 short evidence dimensions the answer must cover (optional).

Decide from the QUESTION'S INTENT, not keywords. When genuinely unsure, choose "balanced". Output ONLY
the JSON object."""


# CONTRACT-RENDERED COMPOSE prompt (ROSTER_CONTRACT_COMPOSE). Unlike the base prompt above (mode always
# "exploratory"), this one classifies the ANSWER SHAPE the question asks for — because compose now renders
# it. The load-bearing addition: recognize an ENUMERATIVE ask ("all X", "every Y", "build me a table",
# "break down into categories", "compare across the field") as enumerative EVEN WHEN the items are not
# named — the row items are then DISCOVERED from the evidence. This is the fix for "build me a table of all
# problems", which the base prompt flattened to exploratory. Rule 18: the LLM judges shape from intent.
TECH_CONTRACT_COMPOSE_PROMPT = """You classify a TECH-RESEARCH question to decide what SHAPE of answer it
asks for. Output JSON with:
- `mode`: ONE of:
  - "enumerative" — the question asks you to ENUMERATE / LIST / TABULATE / BREAK DOWN a SET of things, or
    to survey/compare ACROSS a field of multiple items. Signals: "list all…", "every…", "build me a
    table", "what are the … (plural set)", "break this down by category", "map/compare the players/
    approaches/problems/use-cases". CHOOSE THIS EVEN IF the specific items are NOT named in the question —
    a "table of all X" names the DIMENSIONS, not the rows; the rows get discovered from evidence. The
    deliverable is a COMPLETE multi-item breakdown, not a single verdict.
  - "exploratory" — anything else: a focused/direct question, a how/why explanation, a single-subject
    diligence, a yes/no or build-vs-buy DECISION, "assess the moat", "what did X raise". These want a
    direct reasoned answer, NOT a table.
  DECISIVENESS RULE (do not waffle — the same question must classify the SAME way every time): if the
  question asks for a SET of things — "all X", "every X", "list the X", "what are the X (plural)", "tools/
  players/approaches/problems/options for …" — it is ENUMERATIVE, EVEN WHEN it ALSO asks HOW they work,
  their LIMITS, EXAMPLES, or other analytical sub-questions ABOUT that set. Those analytical sub-questions
  are NOT a reason to pick exploratory — they are the `axes` (the table COLUMNS). Only choose exploratory
  when there is NO set to enumerate at all (a single subject, one focused question, a pure decision).
- `entities`: if the question NAMES the specific items to enumerate (e.g. "compare Cursor, Copilot, and
  Cody"), list them (the row items). If the items are NOT named (e.g. "all problems", "every approach"),
  leave `entities` EMPTY — they will be discovered from the evidence. For a non-enumerative question,
  empty.
- `axes`: 0-5 short evidence DIMENSIONS the answer must cover — for an enumerative ask these are the table
  COLUMNS (e.g. "value mechanism", "ROI evidence", "integration complexity", "limitations"). Extract them
  from what the question asks to understand.
- `stance`: ONE of "current" | "established" | "balanced" (as in the base tech classifier: current = the
  latest/newest state; established = proven/foundational; balanced = neither or a mix).

Decide from the QUESTION'S INTENT, not keywords. Output ONLY the JSON object."""


# SUBJECT-KIND addendum — the extra `subject_kind` output key that lets the kernel route the
# entity-scoped open-web probe (flag ROSTER_WEB_ENTITY_OPEN) and the deep company/person readers
# (flags ROSTER_DEEP_COMPANY_READER / ROSTER_DEEP_PEOPLE_READER). Factored into ONE constant so EVERY
# contract prompt that needs it (the base entity variant AND the landscape variant) stays in lockstep:
# a person/single-entity question must classify identically no matter which base prompt is active. The
# judgment is the LLM's (Rule 18): no keyword matching — decide from the question's intent.
_SUBJECT_KIND_ADDENDUM = """

ALSO add one more key to the SAME JSON object:
- `subject_kind`: ONE of:
  - "specific_entity" — the question is DILIGENCE on a SINGLE NAMED company / product / project: what
    it is, how its tech works, its moat/traction/team/funding/risks (e.g. "what is Blazel", "how does
    X's tech work and what's its moat", "is Acme's approach defensible", "tell me about the startup Y").
    A question about ONE named company's FOUNDERS, team, or leadership is diligence on THAT company →
    specific_entity, even when it says "founders" (plural) (e.g. "tell me everything about Traversal.com
    founders", "who runs Acme", "Acme's founding team"). It becomes "general" only when NO single company
    is named (a class/many companies, e.g. "founders of AI SRE startups").
  - "person" — the question is DILIGENCE on a SINGLE NAMED person: biography, role, career history,
    founder/investor background, public views, affiliations, or reputation (e.g. "tell me about Jane
    Roe", "what has Pat Lee built", "what is Sam Altman's background").
  - "general" — anything else: a landscape/population map, a comparison across many players, a how/why
    about a concept, a trend, or any question NOT centered on one named entity.
  Decide from the QUESTION'S INTENT, not keywords. When unsure, choose "general".

WHEN `subject_kind` is "person" OR "specific_entity", ALSO set `entities` to a ONE-element list holding
the exact named subject — the person's name (for "person") or the company/product name (for
"specific_entity"), e.g. entities=["Zain Jaffer"] or entities=["Traversal.com"]. This names the subject
the deep reader will research. For "general" leave `entities` as it was (the landscape categories, or
empty). Output ONLY the JSON object (WITH the extra `subject_kind` key alongside the others)."""


# ENUM-PROBE addendum (flag ROSTER_ENUM_ENTITY_PROBE). Appended to whichever contract prompt is active when
# the flag is on. For an ENUMERATIVE "table of the main X" ask where the user did NOT name the row items,
# the classifier still leaves `entities` EMPTY (rows are evidence-discovered), but ADDITIONALLY proposes the
# concrete named instances it KNOWS are the main/most-relevant items — so retrieval can fire a TARGETED
# search for each and a well-covered flagship isn't crowded out of axis-only retrieval. These are RETRIEVAL
# SEEDS, never the final rows. Rule 18 + grounding: proposing candidates is a judgment about the QUESTION's
# subject space (safe — retrieval + the span-gate decide what's actually grounded and shown). OFF → the
# field is never requested → derivation byte-identical.
TECH_PROBE_ENTITIES_ADDENDUM = """

ALSO, when `mode` is "enumerative" AND you left `entities` EMPTY (the user asked for a SET — "the main X",
"all X", "top X", "a table of X", "compare the X" — without naming the specific items), add ONE more key:
- `probe_entities`: a list of the CONCRETE, SPECIFICALLY-NAMED instances you know are the most prominent
  members of that set (the actual products, companies, models, or tools — e.g. for "the main AI coding
  assistants": ["GitHub Copilot","Cursor","Claude Code","OpenAI Codex","Windsurf",...]). Name the REAL,
  well-known ones by their exact names — these seed a targeted search per item so a prominent member isn't
  missed; do NOT invent names you're unsure exist.
  HOW MANY: for a narrow "the main X" ask, 5-10 is fine. But for a TABLE / COMPARE / "all X" ask that wants
  a many-row comparison, propose a RICH roster — 15-25 of the most notable members, RANKED IN DESCENDING
  ORDER OF PROMINENCE (biggest / most-funded / most-notable first), because a rich named roster IS the
  deliverable there. E.g. for "table of all notable recent AI/tech startups": ["OpenAI","Anthropic","xAI",
  "Mistral AI","Perplexity","Anysphere (Cursor)","Databricks","Anduril","ElevenLabs","Scale AI","Cohere",
  ...]. Name the ones a knowledgeable analyst would include; order by prominence. This does NOT change `entities`
  (still empty — the final rows are discovered from the retrieved evidence, not from this list). For a
  landscape/MAP ask (entities already set to CATEGORIES) or a non-enumerative ask, OMIT `probe_entities`
  or leave it empty. Output ONLY the JSON object (WITH `probe_entities` when applicable)."""


# REFLECTION addendum (flag ROSTER_REFLECTION). Appended to whichever contract prompt is active when
# reflection is on, so the ONE derivation call ALSO returns the "heart of the question" — the user's real
# underlying intent — used to steer retrieval + compose WITHOUT replacing the literal question. Rule 18 +
# grounding: these are judgments about the QUESTION, never assertions of fact about any entity; the
# span-gate still grounds every emitted claim. Emitted only under the flag → OFF derivation is identical.
TECH_REFLECTION_ADDENDUM = """

ALSO reflect on the HEART of the question and add these keys to the SAME JSON object (judge from the
QUESTION ALONE — never assert facts about any company/person/technology; these only shape HOW we answer):
- `intent`: ONE short sentence naming the user's REAL underlying job — the decision or understanding they
  are actually after beneath the literal words (e.g. for "tell me everything about Traversal.com founders"
  → "assess whether Traversal's founding team is credible/experienced enough to back", NOT "list the
  founders"; for a landscape question → "understand the competitive structure and where the durable
  advantage lies well enough to place a bet or build").
- `intent_confidence`: "high" if that intent is unambiguous from the question; "medium" if a reasonable
  inference; "low" if the question is genuinely ambiguous or you would be GUESSING. When "low", keep
  `intent` faithful to the LITERAL question — do NOT invent a deeper intent you are unsure of.
- `answer_brief`: ONE or TWO sentences naming what a GREAT answer MUST deliver to satisfy that intent —
  the specific dimensions/shape it should cover (this guides coverage + framing; it states no facts).
Decide from the question's real intent, not keywords. Output ONLY the JSON object (now also with
`intent`, `intent_confidence`, and `answer_brief`)."""


# ENTITY-OPEN variant (flag ROSTER_WEB_ENTITY_OPEN). Byte-identical derivation to TECH_CONTRACT_PROMPT
# PLUS the `subject_kind` key. Built by concatenation so it stays in lockstep with the base prompt.
TECH_CONTRACT_PROMPT_ENTITY = TECH_CONTRACT_PROMPT + _SUBJECT_KIND_ADDENDUM


# Per-stance policy knobs (opaque to the kernel). See roster_kernel/contract/manifest.py::answer_profiles.
# CURRENCY FIX (3-panel: web starvation, not stale corpus — session had web cited 7/7 = 100% landing):
# recency was too weak (weight 0.5, 1yr horizon) to lift a 2-day web hit over a similar 4-month Wikipedia
# block. weight 1.0 + ~90-day horizon makes fresh MATHEMATICALLY dominate the cosine baseline (authority
# is already suppressed for this stance, so Wikipedia only wins on volume — now the recency boost + tight
# web floor + wider web reach fix that). Env-overridable so prod can tune without a redeploy.
import os as _os
_CURRENT_RECENCY = {"min_rank": 0, "weight": 1.0, "horizon_years": 0.25}   # strong, ~90-day-horizon recency

ANSWER_PROFILES: dict = {
    "current": {
        "recency": _CURRENT_RECENCY,
        "suppress_authority": True,        # a fresh announcement must be able to out-rank an older benchmark
        # web_open deliberately OFF: fully dropping the whitelist surfaced content-farm/SEO junk
        # (icreat.ai, swfte.com…) that out-ranked authoritative pages. The EXPANDED whitelist now
        # includes the labs' own announcement blogs (openai.com/anthropic.com/blog.google/x.ai…) +
        # leaderboards + trade press, which — with the deep discover→drill research below — already
        # reaches the newest models (GPT-5.6/Opus 5/Kimi K3/DeepSeek-V4-Pro) via CREDIBLE sources.
        "web_open": False,
        # WEB FLOOR: 150d (~5 months) was LOOSER than the 120d Exa baseline — for "what happened last
        # week" it let stale-quarter pages fill the candidate set. Tighten to ~30d so the web leg returns
        # THIS MONTH's deals/launches newest-first. Env-tunable (ROSTER_CURRENT_WEB_RECENCY_DAYS).
        "web_recency_days": int(_os.environ.get("ROSTER_CURRENT_WEB_RECENCY_DAYS", "14")),
        # WEB REACH: the main web leg is otherwise hard-capped at 8 results/query (too few — the binding
        # constraint given 100% web landing). Raise it for CURRENT questions only (scoped via this profile,
        # NOT the global ROSTER_WEB_MAX_RESULTS which regressed latency across all queries). Env-tunable.
        "web_max_results": int(_os.environ.get("ROSTER_CURRENT_WEB_MAX", "30")),
        "max_steps": 14,                   # thorough: discover the leaderboard, then drill into each model
        "compose_claim_cap": 60,           # a landscape answer spans ~15 models, not a handful
        "planner_steer": (
            "This is a CURRENT / LATEST-STATE question. Research it THOROUGHLY, like an analyst: FIRST "
            "search the current model leaderboard(s) and THIS MONTH's releases/announcements; then, for "
            "EACH leading provider and EACH newest model you discover (e.g. the specific latest models "
            "from OpenAI, Anthropic, Google, xAI, Meta, DeepSeek, Moonshot/Kimi, Qwen, Mistral, Z.ai), "
            "issue a SEPARATE targeted search for that exact model/provider to get its own page. Do not "
            "stop after one or two overview searches — cover the whole field."),
        "answer_directive": (
            "REGIME = CURRENT STATE — produce a COMPREHENSIVE, well-structured landscape, not a short "
            "memo. Lead with the newest developments. Include: (1) a TIERED leaderboard TABLE of the "
            "current frontier models (provider · model · rough standing · what stands out), ordered by "
            "current standing; (2) a per-provider read of who leads and each provider's distinct "
            "strength; (3) a short, clearly-labeled OUTLOOK synthesized ONLY from the cited facts. "
            "CURRENCY DISCIPLINE: label each model's status from its cited source — 'available', "
            "'announced', or 'on the leaderboard on paper' — and NEVER present an un-benchmarked release "
            "as if it had verified benchmark results. Cover as many current models as the evidence "
            "supports; do not lead with a prior generation merely because it is more benchmarked."),
    },
    "established": {
        "recency": None,                   # no recency boost — age is not a virtue here
        "suppress_authority": False,       # authority-first: keep the evidence-tier ranking
        "web_recency_days": None,
        "planner_steer": (
            "This question is about ESTABLISHED, proven knowledge. Prefer peer-reviewed papers, "
            "reproducible benchmarks, primary filings, and well-reviewed sources over news or "
            "announcements; foundational/seminal work is welcome regardless of age."),
        "answer_directive": (
            "REGIME = ESTABLISHED KNOWLEDGE. Prioritize the best-verified, benchmarked, peer-reviewed, "
            "primary evidence; foundational/seminal work is welcome regardless of age. Treat unverified "
            "announcements or news as low-weight signal, clearly labeled — never as established fact."),
    },
    # "balanced" is intentionally absent → no profile matches → the kernel keeps today's behavior. Kept
    # here as documentation; an explicit no-op entry would behave identically.
}


# LANDSCAPE-COVERAGE contract (flag ROSTER_LANDSCAPE_COVERAGE). Same one-call contract, but for a
# "map the landscape / examine ALL X / cluster companies / who is building" question it returns
# mode="enumerative" with the conceptual CATEGORIES as `entities` — so the kernel fans retrieval out
# per category (entity×axis legs) instead of a few narrow searches. GUARDRAIL (Rule 18 + grounding):
# the categories are a conceptual FRAME derived from knowledge (safe — a frame is not a fact); the
# companies, founders, funding, and stage are NEVER emitted here and must be extracted from retrieved
# evidence downstream. Non-landscape questions stay exploratory (identical to TECH_CONTRACT_PROMPT).
TECH_LANDSCAPE_CONTRACT_PROMPT = """You classify a TECH-RESEARCH question and, for LANDSCAPE/POPULATION
questions, plan its coverage. Output JSON with `mode`, `entities`, `axes`, `stance`.

FIRST decide `mode`:
- "enumerative" — the question asks to ENUMERATE / MAP / TABULATE / COMPARE a SET of things across
  dimensions. There are TWO kinds, and they take DIFFERENT rows — decide which the question wants:
  (A) LANDSCAPE / POPULATION MAP — "examine all X", "map the landscape", "cluster the companies/
      startups", "who is building X", "the whole market for X", "where's the whitespace", a broad survey
      of a FIELD's structure. Here the natural rows are CONCEPTUAL SEGMENTS: set `entities` to the 6-10
      CATEGORIES the landscape breaks into (the economic segments / sub-fields a knowledgeable analyst
      would use to partition it — NOT company names). These categories are a search frame, not an answer:
      name the SEGMENTS, never specific companies/products/founders/funding here.
  (B) NAMED-ENTITY COMPARISON TABLE — "build a table of the main/top/leading X", "compare Cursor,
      Copilot, and Cody", "list the X and their pricing/model", "the main players and their <attributes>".
      Here the user wants the CONCRETE NAMED INSTANCES (the actual products/companies/models/tools) each
      as its OWN row — NOT lumped into segments. You usually CANNOT name them reliably from the question
      alone (guessing risks inventing ones that don't exist), so LEAVE `entities` EMPTY — the concrete
      rows get discovered from the retrieved evidence downstream. Only fill `entities` if the question
      itself names the specific items to compare.
  In BOTH cases set `axes` to the DIMENSIONS to compare across (e.g. pricing, underlying model, moat,
  differentiation, stage, funding, founders) — 3-6 short phrases.
  WHICH KIND? DECISIVE RULE: if the requested OUTPUT is a COMPARISON TABLE whose rows are companies/
  products/players and whose columns are METRICS/ATTRIBUTES (ARR, customers, funding, valuation, team,
  market share, pricing, model…), the rows are NAMED ENTITIES → case (B), `entities` EMPTY. This holds
  EVEN when the ask says "all X" or "every X" or names a whole population ("a table of all startups in the
  last 3 years, compared on ARR/customers/funding") — a metric-comparison table wants named companies,
  NOT economic segments. Case (A) categories is ONLY for an ask that explicitly wants to MAP / SEGMENT /
  CLUSTER the field or find the WHITESPACE / market STRUCTURE ("map the AI landscape", "what are the
  segments of X"). When both could fit, prefer (B). Never lump a metric-comparison table into (A).
- "exploratory" — anything else (a normal question, a single-entity ask, a how/why, a lookup). Leave
  `entities` empty.

THEN `stance`: "current" (latest/newest/who-leads-now/recent funding) | "established" (proven/
benchmarked/how-it-works/foundational) | "balanced" (mixed/unsure). Decide from INTENT, not keywords.

Output ONLY the JSON object. For an enumerative landscape question the categories must be genuine
distinct segments (e.g. for "AI startups": frontier models, coding agents, horizontal enterprise agents,
vertical AI, AI search/knowledge, voice/multimodal, AI infrastructure, physical AI/robotics, defense/
industrial AI, generative media) — pick the ones that actually fit the specific question's scope."""


# LANDSCAPE + SUBJECT-KIND variant. When the landscape-coverage flag is on, the app swaps the ACTIVE
# contract prompt to the landscape prompt (app.py) — which, without this, DROPS the `subject_kind` key
# and so silently disables the deep company/person readers for EVERY question (a single-entity or person
# ask under landscape coverage could never route a deep read). This variant restores `subject_kind` on
# the landscape path so the two orthogonal concerns — landscape enumeration AND deep-reader routing —
# both work. A landscape/population question stays enumerative with subject_kind="general"; a person or
# single-entity ask stays exploratory (empty entities) and carries subject_kind="person"/"specific_entity"
# to route its deep read. Same lockstep addendum as the base entity variant.
TECH_LANDSCAPE_CONTRACT_PROMPT_ENTITY = TECH_LANDSCAPE_CONTRACT_PROMPT + _SUBJECT_KIND_ADDENDUM
