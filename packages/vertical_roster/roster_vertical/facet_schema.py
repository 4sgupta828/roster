"""The ROSTER facet schema — the canonical dimensions of people, jobs and companies (vertical VOCABULARY;
mechanics live in roster_kernel.facets). docs/specs/facet-contract-evaluator.md §2.3.

Shared keys carry ONE vocabulary for person and job, so a Talent Map and a Job Map navigate identically and
a résumé or a posting compiles to a contract against either kind. `guidance` is the one line the extractor
and the compiler prompts show per key — the model chooses from the vocabulary; code validates; anything the
text does not say is `unknown`."""
from __future__ import annotations

from roster_kernel.facets import FacetKey, FacetSchema, FacetType, FacetWeights

PJ = ("person", "job")

FIELDS = ("software", "data_ml", "hardware", "product", "design", "clinical_pharma", "finance", "sales", "marketing",
          "legal", "people_hr", "operations", "mechanical_civil_electrical", "research", "education", "other")
FUNCTIONS = ("engineering", "research", "product", "design", "sales", "marketing", "recruiting", "finance", "operations",
             "legal", "clinical", "executive", "support", "other")
LEVELS = ("intern", "junior", "mid", "senior", "staff_plus", "leadership")
WORK_TYPES = ("ic", "manager", "executive", "founder", "academic")
WORK_MODES = ("remote", "hybrid", "onsite")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship")
COMPANY_TYPES = ("fortune500", "public", "big_tech", "startup", "other")
# WHAT THE EMPLOYER DOES (owner, 2026-09-07: "why are fintech jobs not captured?"). Read off the COMPANY entity, never
# extracted from a posting or a profile — the same via-key mechanism as company_type / company_stage.
INDUSTRIES = ("fintech", "financial_services", "insurance", "crypto", "healthtech", "biotech_pharma", "healthcare_services", "medical_devices",
              "ecommerce", "marketplace", "retail_consumer", "food_beverage", "travel_hospitality", "enterprise_software",
              "developer_tools", "data_infrastructure", "ai", "security", "social_media", "gaming", "media_entertainment",
              "edtech", "hr_recruiting", "legal_tech", "real_estate_proptech", "logistics_supply_chain", "transportation",
              "automotive", "aerospace_defense", "semiconductors", "hardware_devices", "telecom", "energy_climate",
              "industrial_manufacturing", "construction", "agriculture", "government_public", "education", "nonprofit",
              "professional_services", "staffing", "other")
COMPANY_STAGES = ("pre_seed", "seed", "series_a", "series_b", "series_c_plus", "public")
EVIDENCE_KINDS = ("repos", "papers", "blog_posts", "talks", "patents")

COMP_BANDS = (("under_100k", None, 100_000.0), ("100k_150k", 100_000.0, 150_000.0), ("150k_200k", 150_000.0, 200_000.0),
              ("200k_300k", 200_000.0, 300_000.0), ("300k_plus", 300_000.0, None))
POSTED_BANDS = (("week", None, 7.0), ("month", 7.0, 30.0), ("older", 30.0, None))
YEARS_BANDS = (("0_2", None, 3.0), ("3_5", 3.0, 6.0), ("6_10", 6.0, 11.0), ("11_15", 11.0, 16.0), ("16_plus", 16.0, None))

FACET_SCHEMA = FacetSchema(keys=(
    # ---- shared: what domain, what kind of work, how senior, in what capacity ----
    FacetKey(key="field", type=FacetType.categorical, kinds=PJ, label="Field", values=FIELDS,
             guidance="the DOMAIN the role belongs to (a 'Director of Turbomachinery' is mechanical_civil_electrical; a 'Field CTO' at a software vendor is sales)"),
    FacetKey(key="function", type=FacetType.categorical, kinds=PJ, label="Function", values=FUNCTIONS,
             guidance="the KIND of work done day to day (engineering, research, sales, product, recruiting …), independent of the domain"),
    FacetKey(key="specialty", type=FacetType.set, kinds=PJ, label="Specialty", top_n=10,
             guidance="1–3 short normalized phrases naming the specialty ('distributed systems', 'oncology', 'growth marketing'); none when unclear"),
    FacetKey(key="level", type=FacetType.ordinal, kinds=PJ, label="Level", values=LEVELS,
             guidance="what the title / text STATES: founder / co-founder, CTO, VP, director, head of = leadership; principal / staff / distinguished = staff_plus; senior / lead = senior; an unmarked title is unknown. A 'founding engineer' / 'founding <role>' is an EARLY HIRE, not a founder: its level is whatever else the title states (usually unknown)"),
    FacetKey(key="work_type", type=FacetType.categorical, kinds=PJ, label="Work type", values=WORK_TYPES,
             guidance="ic = individual contributor; manager = manages people; executive = director and above; founder = a founder / co-founder (a 'founding engineer' is ic); academic = faculty / researcher in academia"),
    FacetKey(key="skill", type=FacetType.set, kinds=PJ, label="Skills", top_n=12,
             guidance="up to 8 lowercase skill / tool tokens actually named in the text"),
    FacetKey(key="role_family", type=FacetType.set, kinds=PJ, label="Role", top_n=10, navigable=False,
             guidance="one short normalized role phrase in plain lowercase words ('software engineer', 'cto', 'founding engineer', 'account executive')"),
    # ---- location: the existing facet keys, open vocabulary (hierarchical geo is reserved for v2) ----
    FacetKey(key="country", type=FacetType.set, kinds=PJ + ("company",), label="Country", top_n=12,
             guidance="ISO-ish lowercase country code (us, uk, de, in, ca) when the text places the role or person"),
    FacetKey(key="state", type=FacetType.set, kinds=PJ, label="State", top_n=15, guidance="lowercase US state code when stated (ca, ny, tx)"),
    FacetKey(key="metro", type=FacetType.set, kinds=PJ, label="Metro", top_n=15,
             guidance="normalized metro token (bay_area, new_york, seattle, london) when stated; remote is a work mode, not a place"),
    # ---- jobs only ----
    FacetKey(key="work_mode", type=FacetType.categorical, kinds=("job",), label="Work mode", values=WORK_MODES,
             guidance="remote / hybrid / onsite as the posting states it; a structured board field overrides the text"),
    FacetKey(key="employment_type", type=FacetType.categorical, kinds=("job",), label="Employment", values=EMPLOYMENT_TYPES,
             guidance="full_time unless the posting says part-time, contract, or internship"),
    FacetKey(key="comp", type=FacetType.numeric, kinds=("job",), label="Compensation", unit="usd per year", bands=COMP_BANDS,
             guidance="the stated base pay range as numbers with currency and period (min, max, currency, period) — copy the figures; never estimate; nothing when the posting does not state pay"),
    FacetKey(key="posted", type=FacetType.numeric, kinds=("job",), label="Posted", unit="days ago", bands=POSTED_BANDS, guidance="(derived from the board's posting date; not extracted)"),
    # ---- people only ----
    FacetKey(key="years", type=FacetType.numeric, kinds=("person",), label="Experience", unit="years", bands=YEARS_BANDS,
             guidance="total professional years when dates or the text state them; otherwise unknown"),
    FacetKey(key="evidence", type=FacetType.categorical, kinds=("person",), label="Public evidence", values=EVIDENCE_KINDS,
             guidance="(derived from linked public artifacts; not extracted)"),
    # ---- the employer, and its type / stage read THROUGH the company (facet-through-relation) ----
    FacetKey(key="company", type=FacetType.set, kinds=PJ, label="Company", top_n=12, guidance="the current employer / hiring company as a lowercase slug"),
    FacetKey(key="company_type", type=FacetType.categorical, kinds=PJ, label="Company type", values=COMPANY_TYPES, via="company",
             guidance="(lookup data on the company — never inferred from a posting or profile)"),
    FacetKey(key="company_stage", type=FacetType.ordinal, kinds=PJ, label="Company stage", values=COMPANY_STAGES, via="company",
             guidance="(from the company's own stage facet)"),
    FacetKey(key="company_industry", type=FacetType.categorical, kinds=PJ, label="Industry", values=INDUSTRIES, via="company",
             guidance="what the EMPLOYER does — fintech, healthtech, developer tools … (lookup data on the company, never "
                      "inferred from a posting or a profile; a payments engineer at a bank is financial_services, not fintech)"),
    # ---- the company entity's own keys ----
    FacetKey(key="type", type=FacetType.categorical, kinds=("company",), label="Type", values=COMPANY_TYPES,
             guidance="from curated, dated sets (Fortune 500, public, big tech) and accelerator / stage records; else other"),
    FacetKey(key="stage", type=FacetType.ordinal, kinds=("company",), label="Stage", values=COMPANY_STAGES,
             guidance="the funding stage a registry, accelerator record or filing states"),
))

# EXTRACTION COMPATIBILITY (2026-09-07). `FacetSchema.version()` hashes every key, so adding a VIA key — one that is read
# off a related entity and never extracted from a text — changes the stamp even though nothing about extraction changed.
# The corpus passes re-read a row only when its stamp is not in this set, so a stamp listed here is still trusted.
# ADD a stamp here only when the change genuinely leaves extracted values valid; a vocabulary or guidance change to an
# EXTRACTED key must NOT be listed — those rows have to be read again.
#   77c3be380247 — the schema before `company_industry` (a via key) was added.
COMPATIBLE_EXTRACTION_VERSIONS = ("77c3be380247",)


def extraction_is_current(stamp: str | None) -> bool:
    """True when a stored `schema_version` stamp still describes a valid extraction."""
    return bool(stamp) and (stamp == FACET_SCHEMA.version() or stamp in COMPATIBLE_EXTRACTION_VERSIONS)


# Ranking weights (the small table of judgment the evaluator applies). Prefer / avoid per hit; the centre
# penalty per ordinal step beyond the span. Values mirror the bonuses the surfaces used before unification.
FACET_WEIGHTS = FacetWeights(
    prefer={"field": 0.25, "work_type": 0.20, "function": 0.15, "metro": 0.15, "state": 0.15, "country": 0.10, "work_mode": 0.15, "company_type": 0.12, "company": 0.15, "company_industry": 0.22,
            "skill": 0.06, "role_family": 0.10, "specialty": 0.08, "evidence": 0.10, "level": 0.12},
    avoid={"field": 0.30, "function": 0.15, "level": 0.10, "metro": 0.10, "work_mode": 0.10, "company": 0.15, "company_type": 0.10, "company_industry": 0.25},
    default_prefer=0.10, default_avoid=0.10, center_per_step=0.06, max_hits_per_key=3)

SCHEMA_VERSION = FACET_SCHEMA.version()

# Labels for values the UI shows (only where the token is not self-explanatory).
VALUE_LABELS = {"data_ml": "data / ML", "clinical_pharma": "clinical / pharma", "people_hr": "people / HR",
                "mechanical_civil_electrical": "mechanical / civil / electrical", "staff_plus": "staff+", "ic": "IC",
                "big_tech": "big tech", "fortune500": "Fortune 500", "series_c_plus": "series C+", "under_100k": "under $100k",
                "100k_150k": "$100k–150k", "150k_200k": "$150k–200k", "200k_300k": "$200k–300k", "300k_plus": "$300k+",
                "0_2": "0–2 yrs", "3_5": "3–5 yrs", "6_10": "6–10 yrs", "11_15": "11–15 yrs", "16_plus": "16+ yrs",
                "week": "this week", "month": "this month", "older": "older", "full_time": "full-time", "part_time": "part-time"}
