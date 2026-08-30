"""People-enumeration facet vocabulary + the facet-PARSE prompt (roster domain vocabulary — Rule 18).

The LLM acts as a QUERY COMPILER: it turns a free-text people-discovery question into a normalized
facet filter (it does NOT process the population). Code owns the filter + grounding; the LLM owns only
this semantic normalization (role synonyms, geo rollups, function synonyms). All domain vocabulary
lives HERE, never in the kernel or the app engine.
"""
from __future__ import annotations

# The closed set of facet KEYS the people index filters on. `title` is the raw display; the other keys
# are the normalized, filterable dimensions. `role` = the functional JOB (what they DO), distinct from
# `seniority` (the LEVEL) and `function` (the DOMAIN).
PEOPLE_FACET_KEYS = ("title", "role", "seniority", "function", "metro", "company", "worked_at")

_ROLE = ("software_engineer", "ml_engineer", "system_architect", "solutions_architect", "data_scientist",
         "data_engineer", "research_scientist", "product_manager", "sre", "security_engineer",
         "designer", "devops_engineer", "engineering_manager", "researcher")

# Illustrative normalized VALUES per key (the LLM normalizes to snake_case; this guides it, it is not
# an exhaustive enum — new values are allowed as long as they are normalized consistently).
_SENIORITY = ("c_level", "vp", "director", "principal", "senior_manager", "engineering_manager",
              "staff", "senior", "mid", "junior", "founder")
_FUNCTION = ("machine_learning", "infrastructure", "backend", "frontend", "data", "product",
             "security", "research", "hardware", "design")
_METRO = ("bay_area", "nyc", "seattle", "boston", "los_angeles", "austin", "london", "remote")

PEOPLE_FACET_PARSE_PROMPT = f"""You compile a people-search question into a normalized facet filter.

Output ONLY a compact JSON object mapping facet_key → list of normalized values (snake_case). Include a
key ONLY if the question constrains it. Valid keys: {", ".join(PEOPLE_FACET_KEYS)}.

Normalize synonyms to canonical snake_case values, e.g.:
- role (the functional JOB / expertise — WHAT they do, distinct from level and domain): "System
  Architect" → ["system_architect"]; "Data Scientist" → ["data_scientist"]; "ML Engineer" →
  ["ml_engineer"]; "Solutions Architect" → ["solutions_architect"]; "Product Manager" →
  ["product_manager"]; "Research Scientist" → ["research_scientist"]; "SRE" → ["sre"];
  "researcher(s)"/"scientist(s)"/"academic(s)"/"professor(s)" → ["researcher"]. When the ONLY people
  word is "researchers at <place/field>", emit role=["researcher"] plus the field/company — do NOT put
  "research" in function. Examples: {", ".join(_ROLE)}.
- seniority (canonical values): "Directors" → ["director"]; "EMs"/"Engineering Managers" →
  ["engineering_manager"]; "VPs"/"SVP"/"EVP" → ["vp"]; "C-suite"/"CTO"/"CEO"/"Chief X" → ["c_level"];
  "Head of X" → ["head"]; "Principal"/"Distinguished"/"Fellow" → ["principal"]. A GROUP word maps to
  the SET of leadership levels: "leaders"/"leadership"/"heads"/"execs"/"management" → ["c_level", "vp",
  "director", "head", "senior_manager", "engineering_manager", "principal"]. Examples:
  {", ".join(_SENIORITY)}.
- A ranking word ("top", "best", "most senior", "leading") is NOT a facet — DROP it; the results are
  returned unranked. Only translate the actual role/function/place constraints into facets.
- function (the DOMAIN/field): CANONICALIZE the whole AI/ML family to ["machine_learning"] — "ML",
  "AI", "machine learning", "AI research", "deep learning", "NLP", "computer vision", "LLMs",
  "generative AI" all → ["machine_learning"]. "data science"/"analytics" → ["data_science"]; "infra" →
  ["infrastructure"]; "security" → ["security"]. Map an OCCUPATION word to its field:
  "physicists" → ["physics"]; "biologists" → ["biology"]; "chemists" → ["chemistry"]; "economists" →
  ["economics"]; "neuroscientists" → ["neuroscience"]; "mathematicians" → ["mathematics"];
  "computer scientists" → ["computer_science"]; "medicine"/"medical"/"clinicians"/"physicians" →
  ["medicine"]. NEVER emit "research" as a function — "researcher(s)"/"scientist(s)"/"academic(s)" is a
  ROLE (["researcher"]), NOT a function. Examples: {", ".join(_FUNCTION)}.
- metro: "Bay Area"/"SF"/"San Francisco"/"Silicon Valley"/"Palo Alto" → ["bay_area"];
  "New York" → ["nyc"]. Examples: {", ".join(_METRO)}.
- company: a specific employer → its CANONICAL lowercased short name — drop legal suffixes (Inc, LLC,
  Corp, Platforms) and apply known aliases: "Facebook"/"Meta Platforms" → ["meta"]; "Alphabet"/"Google
  LLC" → ["google"]; "Twitter" → ["x"]; "AWS" → ["amazon"]. E.g. "at Stripe" → {{"company": ["stripe"]}}.
- worked_at (PAST employer / work history — DISTINCT from current company): "worked at Google",
  "previously at Google", "ex-Google", "formerly at", "used to work at", "alumni of Google" →
  {{"worked_at": ["google"]}} (same canonicalization as company). "works at" / "at" / "@" = CURRENT →
  use `company`, NOT `worked_at`.

A single facet may list SEVERAL acceptable values (OR within the key): "Directors or EMs" →
{{"seniority": ["director", "engineering_manager"]}}.

If the question is about ONE SPECIFIC NAMED INDIVIDUAL (identity/profile — e.g. "who is Jane Doe",
"Jane Doe who worked at Acme", "tell me about John Smith the ML director"), do NOT emit facets —
instead set "person" to their full name and "person_context" to any employer/role hints that
disambiguate them (e.g. "Tubi, Netflix, ML infrastructure"). Leave the facet lists empty.

If the question is none of these (a definition, a yes/no, not about people), output exactly {{}}.

Question: {{question}}
JSON:"""


def facet_parse_prompt(question: str) -> str:
    return PEOPLE_FACET_PARSE_PROMPT.replace("{question}", question)
