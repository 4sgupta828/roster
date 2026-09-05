"""LEGACY people vocabulary → the facet schema (vertical VOCABULARY; docs/specs/facet-contract-evaluator.md §8
step 4). The people index carries ~2.3M facet rows in the pre-schema vocabulary (`seniority`, `role`, `function`
in `people_facets.py`'s sense — model-normalized tokens). Until the schema-driven re-extraction runs (gated
spend), this ONE lookup table bridges them, on both sides of the evaluator:

- PROJECTION: stored legacy rows → schema rows (`legacy_person_pairs()` feeds a set-based SQL insert, provenance
  `legacy`; a real extraction replaces them key by key).
- COMPILE: the old engine's compiled legacy filter → a schema contract (`legacy_facets_to_contract`) so the
  Talent rail's counts describe the same slice the engine filtered.

It is a table between two CLOSED vocabularies (the values were model-normalized at ingest), not a reading of
free text — no meaning is inferred here; a token the table does not name maps to nothing (unknown)."""
from __future__ import annotations

# (legacy_key, legacy_value) → {schema_key: schema_value}. A level is never invented for a management or
# academic token that does not state one; a domain is never invented for a bare seniority word.
_LEADERSHIP = {"level": "leadership", "work_type": "executive"}
LEGACY_PERSON_MAP: dict[tuple[str, str], dict[str, str]] = {
    # ---- seniority → level (+ what kind of seat) ----
    ("seniority", "c_level"): _LEADERSHIP, ("seniority", "cto"): _LEADERSHIP, ("seniority", "chief"): _LEADERSHIP,
    ("seniority", "executive"): _LEADERSHIP, ("seniority", "vp"): _LEADERSHIP, ("seniority", "director"): _LEADERSHIP,
    ("seniority", "head"): _LEADERSHIP, ("seniority", "partner"): _LEADERSHIP,
    ("seniority", "founder"): {"level": "leadership", "work_type": "founder"},
    ("seniority", "senior_manager"): {"work_type": "manager"}, ("seniority", "engineering_manager"): {"work_type": "manager"},
    ("seniority", "manager"): {"work_type": "manager"},
    ("seniority", "principal"): {"level": "staff_plus"}, ("seniority", "staff"): {"level": "staff_plus"},
    ("seniority", "distinguished"): {"level": "staff_plus"}, ("seniority", "distinguished_engineer"): {"level": "staff_plus"},
    ("seniority", "architect"): {"level": "staff_plus"},
    ("seniority", "lead"): {"level": "senior"}, ("seniority", "tech_lead"): {"level": "senior"}, ("seniority", "senior"): {"level": "senior"},
    ("seniority", "mid"): {"level": "mid"}, ("seniority", "junior"): {"level": "junior"}, ("seniority", "associate"): {"level": "junior"},
    ("seniority", "intern"): {"level": "intern"},
    ("seniority", "postdoc"): {"work_type": "academic"}, ("seniority", "phd_student"): {"work_type": "academic"},
    ("seniority", "phd_candidate"): {"work_type": "academic"}, ("seniority", "professor"): {"work_type": "academic"},
    ("seniority", "physician"): {"field": "clinical_pharma", "function": "clinical"},
    ("seniority", "product_manager"): {"field": "product", "function": "product"},
    # ---- role (what they do) → field / function / work type ----
    ("role", "researcher"): {"function": "research"}, ("role", "research_scientist"): {"function": "research"},
    ("role", "software_engineer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "software_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "full_stack_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "full_stack_engineer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "fullstack_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "frontend_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "frontend_engineer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "web_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "android_developer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "devops_engineer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "security_engineer"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "sre"): {"field": "software", "function": "engineering", "work_type": "ic"},
    ("role", "system_architect"): {"field": "software", "function": "engineering"},
    ("role", "solutions_architect"): {"field": "software", "function": "engineering"},
    ("role", "data_scientist"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "data_engineer"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "data_analyst"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "ml_engineer"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "machine_learning_engineer"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "ai_engineer"): {"field": "data_ml", "function": "engineering", "work_type": "ic"},
    ("role", "robotics_engineer"): {"field": "hardware", "function": "engineering", "work_type": "ic"},
    ("role", "physician"): {"field": "clinical_pharma", "function": "clinical", "work_type": "ic"},
    ("role", "founder"): {"work_type": "founder", "level": "leadership", "function": "executive"},
    ("role", "executive"): {"work_type": "executive", "level": "leadership", "function": "executive"},
    ("role", "ceo"): {"work_type": "executive", "level": "leadership", "function": "executive"},
    ("role", "cto"): {"work_type": "executive", "level": "leadership", "function": "executive"},
    ("role", "board_member"): {"work_type": "executive", "level": "leadership"},
    ("role", "engineering_manager"): {"work_type": "manager", "function": "engineering"},
    ("role", "product_manager"): {"field": "product", "function": "product"},
    ("role", "designer"): {"field": "design", "function": "design"},
    ("role", "phd_student"): {"work_type": "academic"}, ("role", "student"): {"level": "intern"},
    # ---- function (legacy = technical domain) → field ----
    ("function", "backend"): {"field": "software"}, ("function", "frontend"): {"field": "software"},
    ("function", "full_stack"): {"field": "software"}, ("function", "infrastructure"): {"field": "software"},
    ("function", "security"): {"field": "software"}, ("function", "engineering"): {"field": "software"},
    ("function", "blockchain"): {"field": "software"}, ("function", "computer_science"): {"field": "software"},
    ("function", "machine_learning"): {"field": "data_ml"}, ("function", "data_science"): {"field": "data_ml"},
    ("function", "data"): {"field": "data_ml"},
    ("function", "medicine"): {"field": "clinical_pharma"}, ("function", "internal_medicine"): {"field": "clinical_pharma"},
    ("function", "family_medicine"): {"field": "clinical_pharma"},
    ("function", "biology"): {"field": "research"}, ("function", "chemistry"): {"field": "research"},
    ("function", "materials_science"): {"field": "research"}, ("function", "physics"): {"field": "research"},
    ("function", "geology"): {"field": "research"}, ("function", "environmental_science"): {"field": "research"},
    ("function", "mathematics"): {"field": "research"}, ("function", "optics"): {"field": "research"},
    ("function", "humanities"): {"field": "research"}, ("function", "psychology"): {"field": "research"},
    ("function", "political_science"): {"field": "research"},
    ("function", "hardware"): {"field": "hardware"}, ("function", "robotics"): {"field": "hardware"},
    ("function", "product"): {"field": "product"}, ("function", "design"): {"field": "design"},
    ("function", "sales"): {"field": "sales", "function": "sales"}, ("function", "marketing"): {"field": "marketing", "function": "marketing"},
    ("function", "finance"): {"field": "finance", "function": "finance"}, ("function", "operations"): {"field": "operations", "function": "operations"},
}

# Keys whose legacy rows ARE schema rows already (same key, open vocabulary): no projection needed.
PASS_THROUGH_KEYS = ("metro", "state", "country", "company", "skill")

# `rs_person_artifact.kind` → the `evidence` facet (derived from linked public artifacts, spec §2.3).
ARTIFACT_EVIDENCE = {"paper": "papers", "repo": "repos", "patent": "patents", "post": "blog_posts", "talk": "talks"}


def legacy_person_pairs() -> list[tuple[str, str, str, str]]:
    """The table flattened for a set-based projection: (legacy_key, legacy_value, schema_key, schema_value)."""
    out = []
    for (ok, ov), targets in LEGACY_PERSON_MAP.items():
        for nk, nv in targets.items():
            out.append((ok, ov, nk, nv))
    return out


def _slug(v) -> str:
    return str(v or "").strip().lower().replace(" ", "_")


def legacy_facets_to_contract(query_facets: dict) -> dict:
    """A compiled legacy filter (key → OR-list of values) → {must, prefer} in schema keys, SOUND with respect to
    the legacy filter: within one legacy key, only the schema keys that EVERY chosen value maps to become musts
    (values unioned); a schema key only some values map to only ranks (prefer). Across legacy keys, musts AND.
    Unknown tokens map to nothing."""
    must: dict[str, list[str]] = {}
    prefer: dict[str, list[str]] = {}

    def _add(d: dict, k: str, vals):
        cur = d.setdefault(k, [])
        for v in vals:
            if v not in cur:
                cur.append(v)

    for lk, vals in (query_facets or {}).items():
        if not isinstance(vals, (list, tuple)):
            continue
        toks = [_slug(v) for v in vals if _slug(v)]
        if not toks:
            continue
        if lk in PASS_THROUGH_KEYS:
            _add(must, lk, toks)
            continue
        mapped = [LEGACY_PERSON_MAP.get((lk, t)) for t in toks]
        mapped = [m for m in mapped if m]
        if not mapped:
            continue
        common = set(mapped[0].keys())
        for m in mapped[1:]:
            common &= set(m.keys())
        for m in mapped:
            for nk, nv in m.items():
                _add(must if nk in common else prefer, nk, [nv])
    return {"must": must, "prefer": prefer}


def legacy_brief_to_contract(hard: dict, soft: dict, fallback: dict | None = None) -> dict:
    """The old engine's interpreted brief (what FILTERED vs what only RANKED) → {must, prefer}: hard facets
    translate as `legacy_facets_to_contract` does; soft facets only ever rank, whatever they map to. With no
    brief contract at all, the compiled filter (`fallback`) is read as all-hard."""
    if not hard and not soft:
        return legacy_facets_to_contract(fallback or {})
    out = legacy_facets_to_contract(hard or {})
    s = legacy_facets_to_contract(soft or {})
    prefer = dict(out["prefer"])
    for d in (s["must"], s["prefer"]):
        for k, vals in d.items():
            cur = prefer.setdefault(k, [])
            for v in vals:
                if v not in cur and v not in (out["must"].get(k) or []):
                    cur.append(v)
    return {"must": out["must"], "prefer": {k: v for k, v in prefer.items() if v}}
