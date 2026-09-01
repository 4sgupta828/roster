"""People-population ANSWER engine — the fix for people-DISCOVERY/enumeration questions.

"Find all people where role∈{Director,EM} ∧ function=ML ∧ location=Bay Area" cannot be answered by a
web-RAG retrieval sample (there is no page listing them). It is answered by FILTERING a grounded
people index:

    parse facets (LLM = query COMPILER, not data processor)  [LLM owns the semantic normalization]
      → enumerate_by_facets (SQL AND across keys / OR within a key)  [code owns the filter]
      → grounded people_rows (each attribute cites the claim/evidence it came from)  [grounding intrinsic]
      → HONEST coverage_basis (index scope, matches, sources ingested vs missing — never "all people")
      → an empty match is a SUCCESSFUL honest result (a data gap), NEVER a silent web-RAG fallback.

Flag-gated at the app boundary (ROSTER_PEOPLE_POPULATION). Rule 18: the LLM owns intent/normalization;
code owns the filter, the citation, and the coverage facts. Public-data only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request

from pydantic import BaseModel

_log = logging.getLogger(__name__)

# Sources the index does NOT yet cover — stated in every answer so "find all" never implies the whole
# world. (A curation frontier, not a bug: coverage grows per ingested source.)
NOT_INGESTED = ["LinkedIn", "X/Twitter", "most company org charts"]


class _FacetParse(BaseModel):
    """Fixed schema for the LLM facet compiler (dynamic dicts don't schema cleanly). Empty lists =
    the question did not constrain that facet; ALL empty (and no `person`) = not a people query.
    `person` is set ONLY when the question is about ONE specific named individual (identity/profile),
    with `person_context` carrying any employer/role hints that disambiguate them."""
    role: list[str] = []
    seniority: list[str] = []
    function: list[str] = []
    industry: list[str] = []
    metro: list[str] = []
    company: list[str] = []
    worked_at: list[str] = []
    country: list[str] = []
    state: list[str] = []
    stage: list[str] = []
    accelerator: list[str] = []
    skill: list[str] = []
    person: str = ""
    person_context: str = ""


def semantic_enabled() -> bool:
    """Flag (default OFF, Rule 20): DEEP semantic search — filter by ALL facets (attributes), then rank
    by OpenAI-embedding similarity (the eigen/noesis typed-block pattern). OFF or no OPENAI_API_KEY →
    the exact facet path (byte-identical)."""
    return (os.environ.get("ROSTER_SEMANTIC", "").lower() in ("1", "true", "yes")
            and bool(os.environ.get("OPENAI_API_KEY")))


def people_semantic_first_enabled() -> bool:
    """Flag (default OFF, Rule 20): SEMANTIC-FIRST people retrieval. Rank ALL people by query→profile
    embedding similarity; apply ONLY country as a hard filter (drop just the known-foreign — unknown
    keeps the person), and treat every other parsed facet (skill/function/role/metro/…) as a SOFT
    boost, never a hard gate. This is the match_jd_people philosophy applied to free-text people
    search — the fix for a query compressing into a sparse hard facet and strangling recall (the
    'ML Feature Engineering' → 7 generic SWEs miss). OFF → the existing facet-filter-first path
    (byte-identical). Needs an embedding, so effectively also requires OPENAI_API_KEY."""
    return os.environ.get("ROSTER_PEOPLE_SEMANTIC_FIRST", "").lower() in ("1", "true", "yes")


def embed_query(text: str) -> str | None:
    """Embed the query with text-embedding-3-small → a pgvector literal '[...]'. None on any failure
    (the caller falls back to the exact facet path). Never raises to the route."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not (text or "").strip():
        return None
    try:
        body = json.dumps({"model": "text-embedding-3-small", "input": [text[:2000]]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            v = json.load(r)["data"][0]["embedding"]
        return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
    except Exception as e:  # noqa: BLE001
        _log.warning("embed_query failed: %s", e)
        return None


# ---- Résumé → matched jobs, preference-ranked -------------------------------------------------------
# A compact curated Fortune-500 / big-public-employer slug set (most F500 are public companies). Used
# only to TAG a job's company for optional preference bonuses — never as a hard gate.
_F500 = {
    "walmart","amazon","apple","cvshealth","unitedhealth","exxonmobil","berkshirehathaway","alphabet",
    "google","mckesson","chevron","att","ford","gm","generalmotors","costco","cigna","microsoft","cardinalhealth",
    "meta","facebook","comcast","phillips66","valero","dell","target","fanniemae","ups","lowes","jpmorgan",
    "jpmorganchase","fedex","humana","wellsfargo","citigroup","citi","pepsico","procterandgamble","pg","disney",
    "walgreens","boeing","tesla","nvidia","intel","ibm","oracle","cisco","pfizer","merck","abbvie","johnsonandjohnson",
    "jnj","coca","cocacola","abbott","broadcom","qualcomm","amd","salesforce","adobe","netflix","paypal","starbucks",
    "nike","mcdonalds","americanexpress","goldmansachs","morganstanley","blackrock","3m","mmm","honeywell","caterpillar",
    "lockheedmartin","rtx","deere","ge","hp","hpe","dell","texasinstruments","ti","micron","appliedmaterials",
    "generalmills","kraftheinz","mondelez","colgate","kimberly_clark","clorox","unitedairlines","delta","americanairlines",
    "marriott","hilton","accenture","deloitte","pwc","ey","kpmg","capitalone","americanexpress","visa","mastercard",
    "servicenow","intuit","booking","uber","airbnb","doordash","spotify","snap","pinterest","zoom","workday",
    "verizon","tmobile","charter","progressive","allstate","travelers","metlife","prudential","statestreet",
}
_SEN_RE = [
    (re.compile(r"\b(intern|internship|co-?op)\b", re.I), "intern"),
    (re.compile(r"\b(junior|jr\.?|entry[- ]?level|new ?grad|graduate|associate)\b", re.I), "junior"),
    (re.compile(r"\b(principal|staff|distinguished|fellow)\b", re.I), "staff_plus"),
    (re.compile(r"\b(director|vp|vice ?president|head of|chief|cto|ceo|cfo)\b", re.I), "leadership"),
    (re.compile(r"\b(senior|sr\.?|lead)\b", re.I), "senior"),
]
def _title_seniority(title: str) -> str:
    for rx, lvl in _SEN_RE:
        if rx.search(title or ""):
            return lvl
    return "mid"

# job-location → country, from explicit geography tokens (parse, not semantic inference). Used to
# honor the country scope in match; a location we can't place stays ambiguous and is NOT dropped.
_COUNTRY_RX = {
    "us": re.compile(r"\b(united states|u\.?s\.?a?\.?|usa)\b|,\s*(A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|"
                     r"LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]|DC)\b", re.I),
    "uk": re.compile(r"\b(united kingdom|england|scotland|wales|\buk\b|london|manchester|edinburgh|cambridge, uk)\b", re.I),
    "ca": re.compile(r"\b(canada|toronto|vancouver|montreal|ottawa|waterloo|ontario|quebec|british columbia)\b", re.I),
    "de": re.compile(r"\b(germany|berlin|munich|münchen|hamburg|frankfurt|cologne)\b", re.I),
    "fr": re.compile(r"\b(france|paris|lyon|toulouse)\b", re.I),
    "in": re.compile(r"\b(india|bengaluru|bangalore|hyderabad|mumbai|delhi|pune|chennai|gurgaon|noida)\b", re.I),
    "jp": re.compile(r"\b(japan|tokyo|osaka|kyoto)\b", re.I),
}
def _job_country(loc: str) -> str:
    for c, rx in _COUNTRY_RX.items():
        if rx.search(loc or ""):
            return c
    return ""
def _country_ok(loc: str, want: str) -> bool:
    if not want:
        return True
    jc = _job_country(loc)
    return True if jc == "" else jc == want   # drop ONLY jobs clearly in a DIFFERENT country


async def match_resume_jobs(store, profile: dict, prefs: dict) -> dict:
    """On-demand: embed the user's résumé profile → most-similar jobs → re-rank by explicit preferences
    (location/remote, seniority, role keywords, company type F500/public/startup, best-effort salary).
    Honest: salary is present on only a small % of jobs; company 'stage' is startup-vs-public only."""
    prefs = prefs or {}
    # 1) build a résumé query string. The RAW résumé text (when present) is the richest signal — it
    #    works even when structured extraction is sparse (e.g. a one-line "Founder" title). Structured
    #    fields are appended as reinforcement.
    parts = [str(profile.get("_resume_text", "")),
             profile.get("summary", ""), profile.get("current_title", ""),
             " ".join(profile.get("skills", []) if isinstance(profile.get("skills"), list) else [])]
    for w in (profile.get("work_history") or [])[:4]:
        if isinstance(w, dict):
            parts.append(" ".join(str(w.get(k, "")) for k in ("title", "company", "description")))
    for k in ("field_of_study", "highest_degree"):
        parts.append(str(profile.get(k, "")))
    qtext = " ".join(p for p in parts if p).strip()
    # RECRUITER BRIEF (flag): a recency-weighted, multi-dimensional read of the candidate (the "soul"
    # of the résumé) is a far better retrieval query than raw résumé text — use it as the query when the
    # endpoint supplied one. Falls back to the raw text (byte-identical) when absent.
    brief_text = str(prefs.get("brief_text") or "").strip()
    if brief_text:
        # BLEND, don't replace: the recruiter brief leads (recency-weighted narrative), but keep the raw
        # résumé text too so niche technical vocab (frameworks, project names) still drives the embedding.
        qtext = (brief_text + "\n\n" + qtext).strip() if qtext else brief_text
    if not qtext:
        return {"jobs": [], "note": "Add or parse a résumé first — no profile content to match on."}
    qvec = embed_query(qtext)
    if not qvec:
        return {"jobs": [], "note": "Matching is unavailable right now."}
    cands = await store.match_jobs_scored(qvec, cap=int(prefs.get("candidate_cap", 400)))
    if not cands:
        return {"jobs": []}
    # 2) company-type sets (startup from accelerator/stage facets; f500/public from the curated set)
    startup = await store.companies_with_facet(("accelerator",)) | \
              await store.companies_with_facet(("stage",), ["startup"])
    want = set(prefs.get("company_types") or [])          # subset of {f500,public,startup}
    locs = [str(l).lower() for l in (prefs.get("locations") or []) if str(l).strip()]
    want_remote = bool(prefs.get("remote"))
    # seniority is MULTI-select (list); keep single `seniority` for back-compat
    want_sens = {str(s).lower() for s in (prefs.get("seniorities") or []) if str(s).strip()}
    if prefs.get("seniority"):
        want_sens.add(str(prefs["seniority"]).lower())
    role_kw = [str(k).lower() for k in (prefs.get("role_keywords") or []) if str(k).strip()]
    want_country = (prefs.get("country") or "").lower()
    dropped_country = 0
    # 3) score = semantic similarity + preference bonuses (with human-readable reasons)
    out = []
    for j in cands:
        title, loc, co = j.get("title") or "", (j.get("location") or "").lower(), (j.get("company") or "")
        if not _country_ok(loc, want_country):     # honor the country scope — drop clearly-foreign jobs
            dropped_country += 1; continue
        sim = float(j.get("sim") or 0.0)
        score, reasons = sim, []
        is_f500 = co in _F500
        is_startup = co in startup
        is_public = is_f500 and not is_startup
        if want_remote and "remote" in loc:
            score += 0.15; reasons.append("remote")
        if locs and any(l in loc for l in locs):
            score += 0.15; reasons.append("location")
        jsen = _title_seniority(title)
        if want_sens and jsen in want_sens:
            score += 0.12; reasons.append(f"{jsen.replace('_', ' ')} level")
        if role_kw and any(k in title.lower() for k in role_kw):
            score += 0.10; reasons.append("role match")
        if "f500" in want and is_f500:
            score += 0.12; reasons.append("Fortune 500")
        if "public" in want and is_public:
            score += 0.08; reasons.append("public company")
        if "startup" in want and is_startup:
            score += 0.12; reasons.append("startup")
        types = [t for t, ok in (("F500", is_f500), ("Startup", is_startup), ("Public", is_public)) if ok]
        out.append({**{k: j.get(k) for k in ("id", "company", "title", "location", "url", "source")},
                    "score": round(score, 4), "match_pct": min(99, round(sim * 100)),
                    "seniority": jsen, "company_types": types, "reasons": reasons})
    out.sort(key=lambda x: -x["score"])
    notes = []
    if want_country:
        notes.append(f"{want_country.upper()} only")
    if prefs.get("min_salary"):
        notes.append("salary is best-effort — most listings don't publish pay")
    return {"jobs": out[: int(prefs.get("limit", 40))],
            "matched_on": qtext[:180], "dropped_out_of_country": dropped_country,
            "note": " · ".join(notes)}


class _CandidateBrief(BaseModel):
    technical_skills: list[str] = []     # ranked, strongest/most-recent first
    tech_breadth: str = ""               # one line on range across stacks/domains
    leadership: str = ""                 # scope of leadership/ownership (team size, org, IC-vs-manager)
    key_contributions: list[str] = []    # concrete things they built/shipped
    major_achievements: list[str] = []   # outcomes/impact (scale, revenue, awards)
    target_roles: list[str] = []         # roles a recruiter would pitch them for (canonical, snake_case)
    seniority: str = ""                  # intern|junior|mid|senior|staff|principal|lead|manager|director|vp|c_level
    search_text: str = ""                # a rich recency-weighted paragraph — the query used to find jobs


async def build_candidate_brief(resume_text: str, profile: dict, llm) -> dict | None:
    """Act as a professional recruiter representing this candidate: read the résumé (WEIGHTING the last
    3–5 years far more than earlier roles) and build a comprehensive, multi-dimensional picture —
    technical skills, tech breadth, leadership, key contributions, major achievements — then the roles +
    seniority to pitch and a rich `search_text` (the 'soul' of the résumé) used as the job-match query.
    LLM-owned (Rule 18). Fail-safe to None on any error so the caller falls back to raw-résumé matching."""
    # CODE-DRIVEN RECENCY (Rule 8): work_history is stored most-recent-first (resume_parser), so slice the
    # top roles into a RECENT block the model is told to weight heavily, rather than trusting it to infer
    # recency from a monolithic blob. Fall back to raw résumé text when there's no structured history.
    wh = profile.get("work_history") if isinstance(profile.get("work_history"), list) else []
    def _role_line(w):
        if not isinstance(w, dict):
            return str(w)
        span = " ".join(str(w.get(k, "")) for k in ("start", "end") if w.get(k))
        return f"{w.get('title','')} @ {w.get('company','')} ({span}) — {w.get('description','')}".strip()
    if wh:
        recent = "\n".join(_role_line(w) for w in wh[:3])
        earlier = "\n".join(_role_line(w) for w in wh[3:8])
        skills = ", ".join(profile.get("skills", []) if isinstance(profile.get("skills"), list) else [])
        text = (f"SUMMARY: {profile.get('summary','')}\n\nRECENT EXPERIENCE (weight this MOST):\n{recent}\n\n"
                f"EARLIER EXPERIENCE (context only):\n{earlier}\n\nSKILLS: {skills}").strip()
    else:
        text = (resume_text or "").strip() or " ".join(
            str(profile.get(k, "")) for k in ("summary", "current_title", "_resume_text"))
    if len(text.strip()) < 40 or llm is None:
        return None
    # The ONLY seniority values the matcher/UI understand (backend _title_seniority + FE SEN_OPTS).
    _SEN = "intern | junior | mid | senior | staff_plus | leadership"
    try:
        comp = await llm.complete(
            system=(
                "You are a top technical recruiter working ON BEHALF OF this candidate to find the best-fit "
                "roles. Build a comprehensive picture, weighting the RECENT EXPERIENCE block far more than the "
                "EARLIER one — recent work defines who they are now. Capture across dimensions: technical_skills "
                "(ranked, recent/strongest first), tech_breadth (range across stacks/domains), leadership (scope "
                "— team size, IC vs manager), key_contributions (what they built/shipped), major_achievements "
                "(impact/scale/outcomes). target_roles: 3–6 job-title phrases a recruiter would pitch them for, "
                "in PLAIN lowercase words (e.g. 'staff machine learning engineer', 'ml infrastructure lead') — "
                f"NOT snake_case. seniority: EXACTLY ONE of [{_SEN}] — no other value. search_text: a rich 4–8 "
                "sentence paragraph capturing the SOUL of this candidate (recent focus, strengths, level, "
                "domains) — the query used to retrieve matching jobs, so make it dense with the signal a great "
                "match shares. Ground everything in the résumé; do not invent."),
            messages=[{"role": "user", "content": text[:12000]}],
            response_format=_CandidateBrief, max_tokens=900)
        b = comp.parsed
        sen = str(b.seniority or "").strip().lower().replace(" ", "_")
        _SEN_OK = {"intern", "junior", "mid", "senior", "staff_plus", "leadership"}
        return {"technical_skills": b.technical_skills[:20], "tech_breadth": b.tech_breadth,
                "leadership": b.leadership, "key_contributions": b.key_contributions[:8],
                "major_achievements": b.major_achievements[:8],
                # PLAIN lowercase role phrases (NOT snake_case) — role_keywords is substring-matched
                # against job titles, so 'staff_ml_engineer' would never match "Staff ML Engineer".
                "target_roles": [str(r).strip().lower() for r in (b.target_roles or []) if str(r).strip()][:6],
                "seniority": sen if sen in _SEN_OK else "",
                "search_text": (b.search_text or "").strip()}
    except Exception as e:   # noqa: BLE001 — never break matching; fall back to raw résumé
        _log.warning("build_candidate_brief failed: %s", e)
        return None


def _person_row_from_facets(r: dict) -> dict:
    """Build a people-card row (same shape as answer_people_population) from a people_by_ids row."""
    facets = r["facets"]
    cite = next(({"document_id": f["document_id"], "block_id": f["block_id"]}
                 for f in facets if f.get("document_id")), None)
    links, attrs = [], []
    for f in facets:
        if f["facet_key"].startswith("link_"):
            links.append({"kind": f["facet_key"][5:], "url": f["display_value"]})
        else:
            attrs.append({"key": f["facet_key"], "display": f["display_value"],
                          "document_id": f["document_id"], "block_id": f["block_id"]})
    return {"entity_id": r["entity_id"], "name": r["name"], "blurb": _person_blurb(attrs),
            "attributes": attrs, "links": links, "citation": cite}


class _JDCompany(BaseModel):
    companies: list[str] = []   # the HIRING company and its common name variants/aliases


async def extract_jd_company(jd_text: str, llm) -> list[str]:
    """LLM-owned (Rule 18 — identifying the hiring entity is meaning, not a keyword match): read a job
    description and return the HIRING company plus its common name variants, each a short lowercased
    token (OpenAI -> 'openai', Alphabet/Google -> ['google','alphabet']). FAIL SAFE to [] on any error
    or when the company isn't clearly stated (never guess) — a miss can only UNDER-exclude, never drop
    the wrong people."""
    jd = (jd_text or "").strip()
    if len(jd) < 20 or llm is None:
        return []
    try:
        comp = await llm.complete(
            system="Identify the company that is HIRING for this job description (the employer posting the "
                   "role — not a customer, partner, or a company merely mentioned). Return that company's "
                   "name and its common variants/aliases, each a short lowercased token. If the hiring "
                   "company is not clearly stated, return an empty list. Never guess.",
            messages=[{"role": "user", "content": jd[:6000]}],
            response_format=_JDCompany, max_tokens=120)
        return [str(c).strip().lower() for c in (comp.parsed.companies or []) if str(c).strip()]
    except Exception:   # noqa: BLE001 — extraction failure must fail safe (no exclusion), never crash
        return []


def _norm_co(s: str) -> str:
    """Structural (Rule 18-safe) company key for COMPARISON only: lowercase, alphanumerics only.
    The semantic call (which company + its aliases) is the LLM's; this is just string matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _co_matches(candidate_norm: str, excl: set[str]) -> bool:
    """True if a candidate's normalized company EXACTLY equals any excluded company (after norm).
    Exact-only on purpose: company norms strip legal suffixes to a clean short name, and the LLM
    supplies real aliases, so equality covers the true variants WITHOUT prefix false-positives
    (a 'meta' exclusion must not drop someone at 'metabase'). A miss under-excludes — never
    wrongly hides a valid candidate. The <3-char guard blocks a stray tiny token."""
    ck = candidate_norm
    if not ck:
        return False
    return any(len(e) >= 3 and ck == e for e in excl)


async def match_jd_people(store, jd_text: str, prefs: dict) -> dict:
    """RECRUITER reverse-match: a job description → ranked candidate PEOPLE (semantic over person
    embeddings), re-ranked by preferences (seniority, location, country). Mirror of match_resume_jobs.

    Source-company exclusion: when `prefs['exclude_companies']` is non-empty, candidates who currently
    work at the hiring company are dropped (a recruiter doesn't want to poach from the client). The
    caller (endpoint) decides whether to populate it, gated on the flag + the recruiter's opt-in."""
    prefs = prefs or {}
    jd = (jd_text or "").strip()
    if len(jd) < 20:
        return {"people_rows": [], "note": "Paste a job description (a sentence or more) to match."}
    qvec = embed_query(jd)
    if not qvec:
        return {"people_rows": [], "note": "Matching is unavailable right now."}
    cands = await store.match_people_scored(qvec, cap=int(prefs.get("candidate_cap", 400)))
    if not cands:
        return {"people_rows": []}
    sim_map = {c["entity_id"]: c["sim"] for c in cands}
    rows = await store.people_by_ids([c["entity_id"] for c in cands])
    want_sens = {str(s).lower() for s in (prefs.get("seniorities") or []) if str(s).strip()}
    want_country = (prefs.get("country") or "").lower()
    locs = [str(l).lower() for l in (prefs.get("locations") or []) if str(l).strip()]
    excl_raw = [str(c).strip() for c in (prefs.get("exclude_companies") or []) if str(c).strip()]
    excl = {_norm_co(c) for c in excl_raw if _norm_co(c)}
    excluded_n = 0
    out = []
    for r in rows:
        facets = r["facets"]
        def fval(k):
            return next((f["value_norm"] for f in facets if f["facet_key"] == k), "")
        sen, country, metro = fval("seniority"), fval("country"), fval("metro")
        if want_country and country and country != want_country:   # drop only when we KNOW it's elsewhere
            continue
        # Hide people at the hiring company — check ALL of the person's company facets (a profile can
        # carry more than one), not just the first, so a current employer listed after a prior one is
        # still caught. Still exact-match per alias, so no false positives.
        if excl and any(_co_matches(_norm_co(f["value_norm"]), excl)
                        for f in facets if f["facet_key"] == "company"):
            excluded_n += 1
            continue
        sim = sim_map.get(r["entity_id"], 0.0)
        score, reasons = sim, []
        if want_sens and sen and sen in want_sens:
            score += 0.12; reasons.append(f"{sen.replace('_',' ')} level")
        if locs and metro and any(l in metro or metro in l for l in locs):
            score += 0.12; reasons.append("location")
        card = _person_row_from_facets(r)
        card["match_pct"] = min(99, round(sim * 100)); card["reasons"] = reasons; card["_score"] = score
        out.append(card)
    out.sort(key=lambda x: -x["_score"])
    for c in out:
        c.pop("_score", None)
    return {"people_rows": out[: int(prefs.get("limit", 40))],
            "note": (want_country.upper() + " only" if want_country else ""),
            "excluded_source_company": ({"companies": excl_raw, "count": excluded_n} if excl else None)}


async def parse_people_facets(question: str, llm) -> tuple[dict[str, list[str]], str, str]:
    """LLM query-compiler: free-text people question → (facet filter, person, person_context).
    `facets` is a normalized enumeration filter (empty when the question is not enumeration); `person`
    is a single named individual (identity/profile question) with `person_context` disambiguating
    hints. All empty = not a people query. Fail safe on any LLM/parse failure (never guess)."""
    from roster_vertical.people_facets import PEOPLE_FACET_KEYS, facet_parse_prompt
    try:
        comp = await llm.complete(
            system="You compile a people-search question into normalized facets, OR identify a single "
                   "named person. Return only the structured object; empty if it is not about people.",
            messages=[{"role": "user", "content": facet_parse_prompt(question)}],
            response_format=_FacetParse, max_tokens=400)
        p = comp.parsed
    except Exception as e:  # noqa: BLE001 — a parse/provider failure must not crash the route
        _log.warning("parse_people_facets failed: %s", e)
        return {}, "", ""
    out: dict[str, list[str]] = {}
    for k in PEOPLE_FACET_KEYS:
        vals = [str(v).strip().lower().replace(" ", "_")
                for v in (getattr(p, k, None) or []) if str(v).strip()]
        if vals:
            out[k] = vals
    return out, (getattr(p, "person", "") or "").strip(), (getattr(p, "person_context", "") or "").strip()


class _JobParse(BaseModel):
    """LLM parse of a JOB search → filters over the public-postings table. `title_keywords` are words
    that would actually appear IN a job TITLE (so 'ML engineer' → ['machine learning','engineer'], not
    'ml_engineer'); `company` is the employer; `location` is a city/region or 'remote'."""
    company: list[str] = []
    title_keywords: list[str] = []
    location: str = ""


async def parse_job_query(question: str, llm) -> dict:
    """Free-text job search → {company, title_keywords, location}. Fail safe to keyword-only on error."""
    prompt = (
        "Parse this JOB SEARCH into a JSON object. `company` = the employer(s), canonical lowercased "
        "short name (Stripe→stripe, Meta→meta) or []. A GROUP or CATEGORY is NOT a company: expand a "
        "well-known group to its member companies (big tech→['google','meta','amazon','microsoft',"
        "'apple','nvidia']; FAANG→['meta','apple','amazon','netflix','google']); for a vague category "
        "(startups, enterprises, fintech companies) leave company=[] and let ranking handle it — never "
        "emit the group phrase itself as a company. `title_keywords` = the words that would appear IN "
        "the job TITLE, expanded to how titles are really written (e.g. 'ML engineer'→['machine "
        "learning','engineer']; 'SWE'→['software','engineer']; 'PM'→['product','manager']; 'sales "
        "roles'→['sales']; 'designer'→['designer']). `location` = a city/region if named, or 'remote', "
        "else ''. Keep title_keywords to the 1-3 essential words. JSON only.\n\nJob search: " + question)
    try:
        comp = await llm.complete(
            system="You parse a job-search query into structured filters. Return only the object.",
            messages=[{"role": "user", "content": prompt}],
            response_format=_JobParse, max_tokens=250)
        p = comp.parsed
        return {"company": [str(x).strip().lower().replace(" ", "_") for x in (p.company or []) if str(x).strip()],
                "title_keywords": [str(x).strip().lower() for x in (p.title_keywords or []) if str(x).strip()],
                "location": (p.location or "").strip()}
    except Exception as e:  # noqa: BLE001
        _log.warning("parse_job_query failed: %s", e)
        return {"company": [], "title_keywords": [w for w in question.lower().split() if len(w) > 2][:3], "location": ""}


# ---- Agentic job search: LLM understands + expands → multi-leg semantic retrieval → rerank ------------
class _JobPlan(BaseModel):
    intent: str = ""
    query_variants: list[str] = []   # alternative phrasings / adjacent titles that surface good matches
    must_have: list[str] = []        # key terms a strong match's title should contain
    company: list[str] = []
    seniority: str = ""
    location: str = ""


async def _llm_job_plan(question: str, llm) -> dict:
    prompt = (
        "Plan an INTELLIGENT job search. Return JSON: `intent` (one sentence — what the seeker really "
        "wants). `query_variants` (3-5 ALTERNATIVE search phrasings AND adjacent job titles that would "
        "surface strong matches — e.g. 'ML infra' → ['machine learning infrastructure engineer','ML "
        "platform engineer','MLOps engineer','distributed training engineer']). `must_have` (2-4 key "
        "terms a strong match's TITLE should contain). `company` (member companies if a GROUP is named "
        "e.g. FAANG/big tech, else []). `seniority` (senior/staff/leadership/… or ''). `location` (a "
        "city, 'remote', or ''). JSON only.\n\nQuery: " + question)
    try:
        comp = await llm.complete(system="You plan an intelligent, multi-angle job search. Return only the object.",
                                  messages=[{"role": "user", "content": prompt}],
                                  response_format=_JobPlan, max_tokens=450)
        p = comp.parsed
        return {"intent": (p.intent or "").strip(),
                "variants": [str(v).strip() for v in (p.query_variants or []) if str(v).strip()][:5],
                "must_have": [str(m).strip().lower() for m in (p.must_have or []) if str(m).strip()][:4],
                "company": [str(c).strip().lower().replace(" ", "_") for c in (p.company or []) if str(c).strip()],
                "seniority": (p.seniority or "").strip().lower(), "location": (p.location or "").strip()}
    except Exception:   # noqa: BLE001
        return {"intent": "", "variants": [], "must_have": [], "company": [], "seniority": "", "location": ""}


async def agentic_job_search(store, question: str, llm, country: str = "us") -> dict:
    """LLM reasons about the query, generates multiple search angles, retrieves for EACH (multi-leg),
    then dedupes and reranks — rewarding cross-angle agreement + must-have title hits. Returns the
    ranked jobs WITH the reasoning (intent + angles + per-job why)."""
    plan = await _llm_job_plan(question, llm)
    legs, seen = [], set()
    for l in [question] + plan["variants"]:
        lk = (l or "").strip().lower()
        if lk and lk not in seen:
            seen.add(lk); legs.append(l.strip())
    legs = legs[:6]
    pool: dict = {}
    for leg in legs:
        qv = embed_query(leg)
        if not qv:
            continue
        for j in await store.match_jobs_scored(qv, cap=60):
            key = (j.get("company") or "", j.get("title") or "", j.get("location") or "")
            sim = float(j.get("sim") or 0.0)
            e = pool.get(key)
            if e is None:
                pool[key] = {"job": j, "best": sim, "legs": 1}
            else:
                e["legs"] += 1
                if sim > e["best"]:
                    e["best"] = sim; e["job"] = j
    want_country = (country or "").lower()
    out, dropped = [], 0
    for e in pool.values():
        j = e["job"]; loc = (j.get("location") or "").lower(); title_l = (j.get("title") or "").lower()
        if not _country_ok(loc, want_country):
            dropped += 1; continue
        score = e["best"] + 0.04 * min(e["legs"] - 1, 3)      # cross-angle agreement
        reasons = []
        hits = [m for m in plan["must_have"] if m in title_l]
        if hits:
            score += 0.08 * len(hits); reasons.append("matches " + ", ".join(hits))
        if e["legs"] > 1:
            reasons.append(f"{e['legs']} search angles")
        out.append({**{k: j.get(k) for k in ("id", "company", "title", "location", "url", "source")},
                    "score": round(score, 4), "match_pct": min(99, round(e["best"] * 100)), "reasons": reasons})
    out.sort(key=lambda x: -x["score"])
    note = ((f"{want_country.upper()} only · " if want_country else "")
            + f"agentic — {len(legs)} angles, {len(pool)} candidates")
    return {"jobs": out[:60], "intent": plan["intent"], "query_angles": legs, "note": note,
            "dropped_out_of_country": dropped}


def build_person_profile_card(name: str, context: str = "") -> dict:
    """A single-person profile card built from EXPLICIT profile searches — GitHub (direct user search),
    X (direct search), and LinkedIn (Google search over name + hints, since LinkedIn has no open
    search). These are navigation aids (searches, clearly labeled), so a person question always
    surfaces their GitHub / X / LinkedIn even when we hold no stored profile for them yet."""
    terms = " ".join(t for t in [name, context] if t)
    g = urllib.parse.quote
    links = [
        {"kind": "github_search", "url": "https://github.com/search?q=" + g(terms) + "&type=users"},
        {"kind": "x_search", "url": "https://x.com/search?q=" + g(terms) + "&f=user"},
        {"kind": "linkedin_search",
         "url": "https://www.google.com/search?q=" + g(terms + " site:linkedin.com/in")},
    ]
    return {"entity_id": "search:" + name, "name": name,
            "attributes": ([{"key": "context", "display": context}] if context else []),
            "links": links, "citation": None}


def _person_blurb(attrs: list[dict]) -> str:
    """A COMPREHENSIVE mini-resume, assembled from every pertinent GROUNDED facet — what the person is,
    what they do, their expertise, their career history, and their context — led by their own bio when
    we have one. No facts are invented; every clause comes from a stored facet.

    Shape: "<Seniority Role> at <Company> — <bio in their own words> — Focus: <fields> · Skills: <tech>
    · Previously: <past employers> · <accelerator-backed> · <stage> · <industry> · <location>"."""
    def g(key):
        return next((a["display"] for a in attrs if a["key"] == key and a.get("display")), "")
    def gall(key):
        seen, out = set(), []
        for a in attrs:
            d = a.get("display")
            if a["key"] == key and d and d.lower() not in seen:
                seen.add(d.lower()); out.append(d)
        return out
    bio = g("title")
    sen, role, comp = g("seniority"), g("role"), g("company")
    funcs, skills, past = gall("function"), gall("skill"), gall("worked_at")
    accel, stage, industry = g("accelerator"), g("stage"), g("industry")
    metro = g("metro")

    segs: list[str] = []
    # 'Mid'/'Junior'/'Entry' are noise as a headline word (and read as a fake title, e.g. "Mid at Acme").
    # Only surface seniority when it's a DISTINGUISHING level; otherwise lead with the role/company.
    _GENERIC_SEN = {"mid", "mid_level", "midlevel", "junior", "entry", "entry_level",
                    "ic", "individual_contributor", ""}
    sen_disp = "" if sen.lower().replace(" ", "_").replace("-", "_") in _GENERIC_SEN else sen
    headline = " ".join(dict.fromkeys(x for x in [sen_disp, role] if x))    # dedupe "Founder Founder"
    if headline and comp:
        segs.append(f"{headline} at {comp}")
    elif headline:
        segs.append(headline)
    elif comp:
        segs.append(f"At {comp}")
    if bio and len(bio) >= 25:                # the person's own words — the richest signal
        segs.append(bio)
    tail: list[str] = []
    if funcs:
        tail.append("Focus: " + ", ".join(funcs[:4]))
    if skills:
        tail.append("Skills: " + ", ".join(skills[:6]))
    if past:
        tail.append("Previously: " + ", ".join(past[:4]))
    ctx = []
    if accel:
        ctx.append(f"{accel}-backed")
    if stage:
        ctx.append(stage)
    if industry:
        ctx.append(industry)
    if metro:
        ctx.append(metro)
    if ctx:
        tail.append(" · ".join(ctx))
    if tail:
        segs.append(" · ".join(tail))
    return " — ".join(segs) or bio


def _facet_summary(facets: dict[str, list[str]]) -> str:
    parts = [f"{k}∈{{{', '.join(v)}}}" if len(v) > 1 else f"{k}={v[0]}"
             for k, v in facets.items()]
    return "; ".join(parts)


def _coverage_basis(facets, stats, matches: int) -> dict:
    return {
        "query_facets": facets,
        "country_scope": (facets.get("country") or [None])[0],   # resolved geo scope (FE echoes it)
        "matches_returned": matches,
        "persons_indexed": stats.get("persons_indexed", 0),
        "source_documents": stats.get("source_documents", 0),
        "facet_coverage": stats.get("facet_coverage", {}),
        "not_ingested": NOT_INGESTED,
        "population_statement": (
            f"These are the grounded matches CURRENTLY in Roster's people index "
            f"({stats.get('persons_indexed', 0)} people from {stats.get('source_documents', 0)} "
            f"public sources) — NOT an exhaustive list of everyone matching. "
            f"Not yet ingested: {', '.join(NOT_INGESTED)}."),
    }


async def answer_people_population(*, question: str, tenant_id: str, store, llm,
                                   scope_country: str = "") -> dict:
    """Answer a people-enumeration question from the grounded people index. Always returns a structured
    result (never raises to the route): a compiled facet filter, grounded rows, and honest coverage.

    `scope_country` (from the top-right selector, flag-gated) HARD-filters results to that country — a
    `country=<scope>` facet is ANDed in, so people we cannot place there are excluded. A country the
    query itself names (compiler-parsed) OVERRIDES the selector default."""
    facets, person, ctx = await parse_people_facets(question, llm)
    if not facets and person:
        # SINGLE-PERSON identity/profile question — the router runs the web bio and attaches this
        # profile card (explicit GitHub/X/LinkedIn search links). kind='person'.
        return {"kind": "person", "not_people_query": False,
                "person_card": build_person_profile_card(person, ctx)}
    stats = await store.people_index_stats(tenant_id=tenant_id)
    if not facets:
        # Not a people query at all — signal the router to fall through to normal research.
        return {"kind": "none", "grounded": False, "not_people_query": True, "people_rows": [],
                "coverage_basis": None, "answer": ""}

    # GEO SCOPE (flag-gated): inject the selector country UNLESS the query already named one (query wins).
    if scope_country and not facets.get("country"):
        facets["country"] = [scope_country]

    # DEEP SEMANTIC SEARCH (flag ROSTER_SEMANTIC): filter by ALL facets (attributes), THEN rank by
    # OpenAI-embedding similarity — the eigen/noesis typed-block pattern (attributes filter, meaning
    # ranks). qvec None (flag off / no key / embed failure) → the exact facet path (byte-identical).
    qvec = embed_query(question) if (semantic_enabled() or people_semantic_first_enabled()) else None
    real_facets = [k for k in facets if k not in ("country", "state", "metro")]
    semantic_used = False
    semantic_first = False
    sf_sim: dict = {}                            # entity_id -> similarity (semantic-first → match_pct)
    if qvec and people_semantic_first_enabled():
        # SEMANTIC-FIRST (flag): meaning leads. Rank ALL people by query→profile similarity; the ONLY
        # hard filter is country (drop just the known-foreign — an unknown country keeps the person, so
        # a sparse facet never strangles recall). Every parsed facet (skill/function/role/metro/…) is a
        # SOFT boost, not a gate — the fix for a query compressing into a sparse hard facet.
        scored = await store.match_people_scored(qvec, cap=500)
        sf_sim = {c["entity_id"]: c["sim"] for c in scored}
        cand = await store.people_by_ids([c["entity_id"] for c in scored], tenant_id=tenant_id)
        want_country = set(facets.get("country") or [])
        boost = {k: set(v) for k, v in facets.items() if k != "country"}     # soft-boost facets
        ranked = []
        for r in cand:
            if want_country:
                cvals = [f["value_norm"] for f in r["facets"] if f["facet_key"] == "country"]
                if cvals and not (set(cvals) & want_country):
                    continue                     # KNOWN-foreign → drop; unknown country → keep (recall)
            rf = {(f["facet_key"], f["value_norm"]) for f in r["facets"]}
            nb = sum(1 for k, vals in boost.items() for v in vals if (k, v) in rf)
            r["_sf"] = sf_sim.get(r["entity_id"], 0.0) + 0.03 * min(nb, 4)   # similarity + soft boost
            ranked.append(r)
        ranked.sort(key=lambda r: -r["_sf"])
        rows = ranked[:200]
        semantic_used = semantic_first = bool(rows)
    elif qvec and real_facets:                   # HYBRID: attribute-filter → semantic-rank within it
        cand = await store.enumerate_by_facets(facets, tenant_id=tenant_id, cap=1000)
        ids = await store.semantic_people(qvec, candidate_ids=[r["entity_id"] for r in cand], cap=200)
        rows = await store.people_by_ids(ids, tenant_id=tenant_id) if ids else cand[:200]
        semantic_used = bool(ids)
    elif qvec:                                   # VIBE query (only geo / no attributes): pure semantic + geo
        ids = await store.semantic_people(qvec, cap=500)
        cand = await store.people_by_ids(ids, tenant_id=tenant_id)
        geo = {k: set(facets[k]) for k in ("country", "state", "metro") if facets.get(k)}
        def _geo_ok(r):
            return all(any(f["facet_key"] == k and f["value_norm"] in vals for f in r["facets"])
                       for k, vals in geo.items())
        rows = [r for r in cand if _geo_ok(r)][:200]
        semantic_used = bool(rows)
    else:
        rows = await store.enumerate_by_facets(facets, tenant_id=tenant_id, cap=200)

    # GRACEFUL PROGRESSIVE RELAXATION: an over-specific query ANDs to zero (e.g. "sales GTM leaders in
    # California" → state=ca matches no business person; "engineers content platform at netflix" →
    # function=content matches nobody). When the full filter is empty, relax in TIERS — drop the sparse
    # GEO narrowing FIRST (least semantic), then skill, then function/industry — keeping the meaning
    # (a sales/engineer intent) as long as possible, and never relaxing down to a geo/country-only
    # filter (which would return "everyone"). Returns the closest honest match + a note of what relaxed.
    _TIERS = [("industry", "metro", "state"), ("skill",), ("function",), ("role", "seniority")]
    _GEO_ONLY = {"country", "state", "metro"}   # never relax down to a geo/country-only filter
    relaxed_from: list[str] = []
    if not rows:
        kept, dropped = dict(facets), []
        for tier in _TIERS:
            drop_now = [k for k in tier if k in kept]
            if not drop_now:
                continue
            for k in drop_now:
                kept.pop(k, None); dropped.append(k)
            if not any(k not in _GEO_ONLY for k in kept):     # nothing meaningful left → stop
                break
            r2 = await store.enumerate_by_facets(kept, tenant_id=tenant_id, cap=200)
            if r2:
                rows, relaxed_from, facets = r2, list(dropped), kept
                break

    coverage = _coverage_basis(facets, stats, len(rows))
    if relaxed_from:
        coverage["relaxed_from"] = relaxed_from
    coverage["semantic_used"] = semantic_used   # observability: did embedding ranking engage?
    coverage["semantic_first"] = semantic_first  # observability: semantic-first (facets soft) vs facet-gated

    people_rows = []
    for r in rows:
        # one representative grounded citation per person (first facet carrying a source)
        cite = next(({"document_id": f["document_id"], "block_id": f["block_id"]}
                     for f in r["facets"] if f.get("document_id")), None)
        # `link_*` facets are the person's OTHER profiles (linkedin/x/website/medium/email), enriched
        # from the source profile — surfaced as clickable links on the card, not filter attributes.
        links, attrs = [], []
        for f in r["facets"]:
            if f["facet_key"].startswith("link_"):
                links.append({"kind": f["facet_key"][5:], "url": f["display_value"]})
            else:
                attrs.append({"key": f["facet_key"], "display": f["display_value"],
                              "document_id": f["document_id"], "block_id": f["block_id"]})
        # LinkedIn PROXY: when we have no direct LinkedIn link, synthesize a Google search over the
        # person's name + role + company that reliably lands on their LinkedIn — a navigation aid
        # (clearly a SEARCH, not grounded evidence). Skipped when a real LinkedIn link exists.
        if not any(l["kind"] == "linkedin" for l in links):
            title_disp = (next((a["display"] for a in attrs if a["key"] == "title"), "")
                          or next((a["display"] for a in attrs if a["key"] == "seniority"), ""))
            company_disp = next((a["display"] for a in attrs if a["key"] == "company"), "")
            terms = " ".join(t for t in [r["name"], title_disp, company_disp, "LinkedIn"] if t)
            links.append({"kind": "linkedin_search",
                          "url": "https://www.google.com/search?q=" + urllib.parse.quote(terms)})
        people_rows.append({
            "entity_id": r["entity_id"], "name": r["name"], "blurb": _person_blurb(attrs),
            "attributes": attrs, "links": links, "citation": cite})

    if semantic_first:                            # surface the relevance score on each card (like JD-match)
        for p in people_rows:
            s = sf_sim.get(p["entity_id"])
            if s is not None:
                p["match_pct"] = max(1, min(99, round(s * 100)))

    # RANK toward the query (user: ranked, not neutral): most prominent first by seniority/tier, then a
    # completeness boost (a contactable, linked profile ranks above a bare one), then name for stability.
    _RANK = {"c_level": 10, "cto": 10, "distinguished_scientist": 9, "vp": 9, "head": 8, "director": 8,
             "senior_manager": 7, "engineering_manager": 7, "lead": 6, "principal": 6, "staff": 5,
             "senior": 4, "researcher": 3, "physician": 3, "mid": 2, "junior": 1, "student": 0}

    def _score(p):
        sen = next((a["display"] for a in p["attributes"] if a["key"] == "seniority"), "")
        base = _RANK.get(sen.lower().replace(" ", "_"), 3)
        return base + min(len(p["links"]), 3) * 0.1   # small boost for a richer/contactable profile

    if not semantic_used:            # semantic search already ranked by query relevance — keep that order
        people_rows.sort(key=lambda p: (-_score(p), p["name"]))

    summary = _facet_summary(facets)
    if people_rows:
        relax_note = (f" (no exact match, so the {', '.join(relaxed_from)} filter"
                      f"{'s were' if len(relaxed_from) > 1 else ' was'} relaxed)" if relaxed_from else "")
        # SEMANTIC-FIRST leads by relevance to the query itself (facets were soft boosts, not a gate),
        # so the honest lead line says so instead of implying a hard facet match.
        if semantic_first:
            lead = f"Top {len(people_rows)} people by relevance to “{question}” in Roster's grounded people index."
        else:
            lead = (f"Found {len(people_rows)} people matching [{summary}]{relax_note} in Roster's "
                    f"grounded people index.")
        lines = [lead, "", coverage["population_statement"], ""]
        for i, p in enumerate(people_rows, 1):
            attrs = ", ".join(a["display"] for a in p["attributes"] if a["display"])
            lines.append(f"{i}. {p['name']} — {attrs}")
        answer = "\n".join(lines)
        grounded = True
    else:
        answer = (f"No people in Roster's index yet match [{summary}]. "
                  + coverage["population_statement"]
                  + " This is a coverage gap, not a dead end — ingest a source that lists these people "
                    "(e.g. company leadership/team pages, conference speakers) to populate it.")
        grounded = False

    return {"grounded": grounded, "not_people_query": False, "answer": answer,
            "people_rows": people_rows, "coverage_basis": coverage}
