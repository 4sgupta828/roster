"""People-enumeration facet vocabulary + the facet-PARSE prompt (roster domain vocabulary — Rule 18).

The LLM acts as a QUERY COMPILER: it turns a free-text people-discovery question into a normalized
facet filter (it does NOT process the population). Code owns the filter + grounding; the LLM owns only
this semantic normalization (role synonyms, geo rollups, function synonyms). All domain vocabulary
lives HERE, never in the kernel or the app engine.
"""
from __future__ import annotations

# The closed set of facet KEYS the people index filters on. `title` is the raw display; the other keys
# are the normalized, filterable dimensions.
PEOPLE_FACET_KEYS = ("title", "seniority", "function", "metro", "company")

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
- seniority: "Directors" → ["director"]; "EMs"/"Engineering Managers" → ["engineering_manager"];
  "VPs" → ["vp"]; "C-suite" → ["c_level"]. A GROUP word maps to the SET of leadership levels:
  "leaders"/"leadership"/"heads"/"execs" → ["c_level", "vp", "director", "senior_manager",
  "engineering_manager"]. Examples: {", ".join(_SENIORITY)}.
- A ranking word ("top", "best", "most senior", "leading") is NOT a facet — DROP it; the results are
  returned unranked. Only translate the actual role/function/place constraints into facets.
- function: CANONICALIZE the whole AI/ML family to ["machine_learning"] — "ML", "AI", "machine
  learning", "AI research", "deep learning", "NLP", "computer vision", "LLMs", "generative AI",
  "ML platform" all → ["machine_learning"]. "data science"/"analytics" → ["data_science"]. "infra" →
  ["infrastructure"]; "security" → ["security"]. Examples: {", ".join(_FUNCTION)}.
- metro: "Bay Area"/"SF"/"San Francisco"/"Silicon Valley"/"Palo Alto" → ["bay_area"];
  "New York" → ["nyc"]. Examples: {", ".join(_METRO)}.
- company: a specific employer → its lowercased name, e.g. "at Stripe" → {{"company": ["stripe"]}}.

A single facet may list SEVERAL acceptable values (OR within the key): "Directors or EMs" →
{{"seniority": ["director", "engineering_manager"]}}.

If the question is NOT a people-discovery/enumeration query (e.g. "who is X", a single named person, a
yes/no, a definition), output exactly {{}}.

Question: {{question}}
JSON:"""


def facet_parse_prompt(question: str) -> str:
    return PEOPLE_FACET_PARSE_PROMPT.replace("{question}", question)
