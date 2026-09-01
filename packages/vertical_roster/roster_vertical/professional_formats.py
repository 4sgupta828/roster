"""Route-specific ANSWER CONTRACTS for professional-intelligence Q&A (vertical-owned vocabulary).

The app-level Q&A router (apps/api/qa_router.py, flag ROSTER_QA_ROUTER) selects one of these as the
per-call `answer_format_override` for ResearchService.ask — the kernel stays domain-free; these are
opaque compose directives exactly like TECH_ANSWER_FORMAT. Shared discipline (the roster grounding
rules, docs/qa_improvements_amended_design.md):
- a section appears ONLY when verified findings support it — never a fabricated heading;
- every factual sentence carries an inline [n] tied to a verified verbatim quote;
- self-reported claims (bios, profiles, company pages) are LABELED as self-reported and kept
  distinct from independent corroboration;
- current vs stale is disclosed ("as of <date>" when the evidence is dated);
- no employment, seniority, credential, or affiliation is ever inferred without evidence.
"""
from __future__ import annotations

_SHARED_RULES = """
Shared rules: include a section ONLY when the verified findings support it — never an empty or
speculative heading. Every factual sentence carries an inline [n] citation tied to a verified
verbatim quote. Label SELF-REPORTED evidence (the person's own bio/profile, a company's own page)
as such, distinct from independent corroboration. Date-sensitive claims (current role, employer,
open roles) carry an "as of" when the evidence is dated; say when a source may be stale. Never
infer employment, seniority, credentials, or affiliation the evidence does not state. Optional
inline markers the UI renders: [[F]]…[[/F]] (hard fact), [[R]]…[[/R]] (reasoning), [[K]]…[[/K]]
(key context)."""

PERSON_DOSSIER_FORMAT = """Write a grounded PERSON DOSSIER from public evidence. Markdown.
Sections, in order, each present only if supported:
- **Bottom line** — 1–3 sentences: who this person is professionally, from verified facts.
- **Identity & current affiliation** — current role/employer with the evidence date; flag any
  conflict between sources (old role vs new role) rather than silently picking one.
- **Career history** — roles/companies the evidence establishes, in order, each cited.
- **Work & contributions** — projects, code, publications, patents, talks; distinguish repositories
  OWNED vs merely contributed to; metrics only when a cited source states them.
- **Public professional footprint** — where they publish/speak/maintain a presence (cited).
- **Collaborators & connections** — co-authors, co-founders, colleagues the evidence names.
- **Evidence gaps & ambiguity** — what public evidence does NOT establish; if multiple people share
  this name, say which identity this dossier follows and why.
NEVER merge claims across possibly-different people with the same name — a mismatched detail
(different employer/field/location) goes to the ambiguity section, not into the dossier.""" + _SHARED_RULES

COMPANY_HIRING_FORMAT = """Write grounded COMPANY HIRING INTELLIGENCE from public evidence. Markdown.
Sections, in order, each present only if supported:
- **Bottom line** — 1–3 sentences on this company as an employer, from verified facts.
- **What the company builds** — product/mission as the evidence states it.
- **Hiring signal & open roles** — open-role evidence with as-of dates; a posting proves the STATED
  requirement/opening, not team practice or actual growth.
- **Engineering & product areas** — teams/areas the evidence names.
- **Tech stack & architecture evidence** — stack claims only from cited engineering sources
  (engineering blogs, talks, repos), never inferred from job-ad keyword lists alone (label those
  as posting-stated).
- **Leadership & team signals** — named leaders/roles the evidence establishes.
- **Evidence gaps & stale-source warnings** — what isn't covered; postings or pages that may be
  outdated.""" + _SHARED_RULES

JOB_DESCRIPTION_FORMAT = """Analyze the JOB DESCRIPTION as citable evidence (the JD text is a
retrievable source — quote it). Markdown. Sections, in order, each present only if supported:
- **Role summary** — what this role is, per the JD.
- **Must-have requirements** — stated hard requirements, quoted.
- **Nice-to-have requirements** — stated preferences, quoted.
- **Leveling & seniority signals** — title/scope/experience signals the JD states.
- **Tech & domain areas** — technologies and domains the JD names.
- **Interview / preparation implications** — reasoning [[R]]…[[/R]] over the stated requirements
  (label as inference; introduce no requirement the JD does not state).
- **Evidence gaps** — what the JD does not specify (comp, team size, level) — a gap, not a guess.
Analyze the SUPPLIED JD text; if no JD text is available beyond a title, say so and ask for the
posting text rather than inventing requirements.""" + _SHARED_RULES

CONNECTION_PATH_FORMAT = """Answer HOW THE ENTITIES ARE CONNECTED from public evidence. Markdown.
Sections, in order, each present only if supported:
- **Direct answer** — connected or not established, in one sentence.
- **Path summary** — the connection chain(s) in words (X → relationship → Y), each hop cited.
- **Per-hop evidence** — for each hop: the relationship and its verbatim supporting quote.
- **Strength of connection** — direct vs via intermediaries; evidence tier per hop.
- **Missing or ambiguous links** — hops that rest on weak/self-reported evidence, and what is not
  established.
If the evidence establishes NO link, say plainly that public evidence reviewed here does not
establish a connection — absence of evidence is a coverage statement, never proof of no
relationship.""" + _SHARED_RULES

# Route → format (the app router indexes this; unknown/None → the vertical default format).
ROUTE_ANSWER_FORMATS = {
    "person_dossier": PERSON_DOSSIER_FORMAT,
    "company_hiring": COMPANY_HIRING_FORMAT,
    "jd_analysis": JOB_DESCRIPTION_FORMAT,
    "connection_path": CONNECTION_PATH_FORMAT,
}
