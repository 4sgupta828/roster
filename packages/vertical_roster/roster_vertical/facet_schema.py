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
             guidance="what the title / text STATES: founder, CTO, VP, director, head of = leadership; principal / staff / distinguished = staff_plus; senior / lead = senior; an unmarked title is unknown"),
    FacetKey(key="work_type", type=FacetType.categorical, kinds=PJ, label="Work type", values=WORK_TYPES,
             guidance="ic = individual contributor; manager = manages people; executive = director and above; founder; academic = faculty / researcher in academia"),
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
    # ---- the company entity's own keys ----
    FacetKey(key="type", type=FacetType.categorical, kinds=("company",), label="Type", values=COMPANY_TYPES,
             guidance="from curated, dated sets (Fortune 500, public, big tech) and accelerator / stage records; else other"),
    FacetKey(key="stage", type=FacetType.ordinal, kinds=("company",), label="Stage", values=COMPANY_STAGES,
             guidance="the funding stage a registry, accelerator record or filing states"),
))

# Ranking weights (the small table of judgment the evaluator applies). Prefer / avoid per hit; the centre
# penalty per ordinal step beyond the span. Values mirror the bonuses the surfaces used before unification.
FACET_WEIGHTS = FacetWeights(
    prefer={"metro": 0.15, "state": 0.15, "country": 0.10, "work_mode": 0.15, "company_type": 0.12, "company": 0.15, "skill": 0.05,
            "role_family": 0.10, "specialty": 0.05, "evidence": 0.10, "level": 0.12},
    avoid={"field": 0.20, "function": 0.10, "level": 0.10, "metro": 0.10, "work_mode": 0.10, "company": 0.15, "company_type": 0.10},
    default_prefer=0.10, default_avoid=0.10, center_per_step=0.06, max_hits_per_key=3)

SCHEMA_VERSION = FACET_SCHEMA.version()

# Labels for values the UI shows (only where the token is not self-explanatory).
VALUE_LABELS = {"data_ml": "data / ML", "clinical_pharma": "clinical / pharma", "people_hr": "people / HR",
                "mechanical_civil_electrical": "mechanical / civil / electrical", "staff_plus": "staff+", "ic": "IC",
                "big_tech": "big tech", "fortune500": "Fortune 500", "series_c_plus": "series C+", "under_100k": "under $100k",
                "100k_150k": "$100k–150k", "150k_200k": "$150k–200k", "200k_300k": "$200k–300k", "300k_plus": "$300k+",
                "0_2": "0–2 yrs", "3_5": "3–5 yrs", "6_10": "6–10 yrs", "11_15": "11–15 yrs", "16_plus": "16+ yrs",
                "week": "this week", "month": "this month", "older": "older", "full_time": "full-time", "part_time": "part-time"}
