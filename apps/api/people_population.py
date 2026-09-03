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

from roster_vertical.people_facets import US_METROS, US_STATES

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
    # TOPIC TERMS: 3–6 short phrases naming the specific domain the brief requires (with synonyms and
    # adjacent terms) — used to ANCHOR results on people whose profile text actually mentions the
    # topic, so a sparse topic (e.g. 'ad serving') is not drowned by generic role look-alikes.
    topic_terms: list[str] = []


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
    # broadened after the F2000 sweep pulled global boards (Bosch/Kioxia/…) — a location we still
    # can't place stays ambiguous and is NOT dropped (the recall rule).
    "es": re.compile(r"\b(spain|madrid|barcelona|valencia)\b", re.I),
    "nl": re.compile(r"\b(netherlands|amsterdam|eindhoven|rotterdam|utrecht)\b", re.I),
    "ch": re.compile(r"\b(switzerland|zurich|zürich|geneva|basel|lausanne)\b", re.I),
    "ie": re.compile(r"\b(ireland|dublin|cork)\b", re.I),
    "se": re.compile(r"\b(sweden|stockholm|gothenburg)\b", re.I),
    "dk": re.compile(r"\b(denmark|copenhagen|aarhus)\b", re.I),
    "no": re.compile(r"\b(norway|oslo)\b", re.I),
    "fi": re.compile(r"\b(finland|helsinki|espoo)\b", re.I),
    "pl": re.compile(r"\b(poland|warsaw|krakow|kraków|wroclaw|gdansk)\b", re.I),
    "pt": re.compile(r"\b(portugal|lisbon|porto)\b", re.I),
    "it": re.compile(r"\b(italy|milan|milano|rome|roma|turin)\b", re.I),
    "at": re.compile(r"\b(austria|vienna|wien)\b", re.I),
    "be": re.compile(r"\b(belgium|brussels|antwerp|ghent)\b", re.I),
    "cz": re.compile(r"\b(czech|prague|brno)\b", re.I),
    "ro": re.compile(r"\b(romania|bucharest|cluj)\b", re.I),
    "hu": re.compile(r"\b(hungary|budapest)\b", re.I),
    "br": re.compile(r"\b(brazil|brasil|s[ãa]o paulo|rio de janeiro|campinas)\b", re.I),
    "mx": re.compile(r"(?<!new )\bmexico\b|\bm[ée]xico\b|\bguadalajara\b|\bmonterrey\b", re.I),
    "ar": re.compile(r"\b(argentina|buenos aires)\b", re.I),
    "co": re.compile(r"\b(colombia|bogot[aá]|medell[ií]n)\b", re.I),
    "sg": re.compile(r"\b(singapore)\b", re.I),
    "kr": re.compile(r"\b(south korea|korea|seoul)\b", re.I),
    "cn": re.compile(r"\b(china|shanghai|beijing|shenzhen|hangzhou|suzhou|nanjing)\b", re.I),
    "tw": re.compile(r"\b(taiwan|taipei|hsinchu)\b", re.I),
    "hk": re.compile(r"\b(hong kong)\b", re.I),
    "au": re.compile(r"\b(australia|sydney|melbourne|brisbane|perth)\b", re.I),
    "nz": re.compile(r"\b(new zealand|auckland|wellington)\b", re.I),
    "il": re.compile(r"\b(israel|tel aviv|haifa|jerusalem)\b", re.I),
    "ae": re.compile(r"\b(united arab emirates|dubai|abu dhabi|uae)\b", re.I),
    "tr": re.compile(r"\b(turkey|t[üu]rkiye|istanbul|ankara)\b", re.I),
    "za": re.compile(r"\b(south africa|cape town|johannesburg)\b", re.I),
    "id": re.compile(r"\b(indonesia|jakarta)\b", re.I),
    "th": re.compile(r"\b(thailand|bangkok)\b", re.I),
    "vn": re.compile(r"\b(vietnam|hanoi|ho chi minh)\b", re.I),
    "my": re.compile(r"\b(malaysia|kuala lumpur)\b", re.I),
    "ph": re.compile(r"\b(philippines|manila)\b", re.I),
}
def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


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


async def widen_jobs_locally(store, rows: list[dict], *, qvec: str | None, terms=None, country: str = "",
                             metro: str = "", state: str = "", query_location: str = "",
                             cap: int = 150) -> list[dict]:
    """LOCAL RECALL for roles: the semantic cohort is drawn before the scope applies, so few of its
    rows are located in the user's metro. Add roles whose location text is inside the scope (ranked
    by the same similarity when we have a query vector), de-duplicated by id / (company,title,loc)."""
    metro, state = (metro or "").lower(), (state or "").lower()
    if (query_location or "").strip() or not (metro or state) or (country or "us").lower() != "us":
        return rows
    try:
        from api.geo import location_regex
        rx = location_regex(metro, state)
        extra = await store.jobs_local(location_regex=rx, qvec=qvec, terms=terms, cap=cap)
    except Exception as ex:  # noqa: BLE001 — additive
        _log.info("local job recall skipped: %s", ex)
        return rows
    seen = {r.get("id") for r in rows if r.get("id") is not None}
    seen_k = {((r.get("company") or ""), (r.get("title") or ""), (r.get("location") or "")) for r in rows}
    out = list(rows)
    for j in extra:
        k = ((j.get("company") or ""), (j.get("title") or ""), (j.get("location") or ""))
        if j.get("id") in seen or k in seen_k:
            continue
        if j.get("sim") is not None:
            j["match_pct"] = min(99, round(float(j["sim"]) * 100))
        out.append(j); seen.add(j.get("id")); seen_k.add(k)
    if any(r.get("sim") is not None for r in out):
        out.sort(key=lambda r: -(float(r.get("sim") or 0.0)))
    return out


def apply_job_scope(rows: list[dict], *, country: str = "", metro: str = "", state: str = "",
                    query_location: str = "", query_company: bool = False) -> tuple[list[dict], dict | None]:
    """Country + LOCAL scope for job rows: drop the clearly-elsewhere, lead with the local, keep
    remote/unplaced after. A location the query names wins (no scope applied). Returns
    (rows, geo_scope|None) where geo_scope carries the label/counts/statement for the UI."""
    if (query_location or "").strip():
        return rows, None
    want_c = (country or "").lower()
    if want_c:
        rows = [r for r in rows if _country_ok(r.get("location") or "", want_c)]
    metro, state = (metro or "").lower(), (state or "").lower()
    if not (metro or state) or want_c not in ("", "us"):
        return rows, None
    from api.geo import job_geo_status, partition_local, scope_label, scope_statement
    if query_company:
        # the user named a company: keep ALL its roles (local first, elsewhere after) — never drop
        inn, rest = [], []
        for j in rows:
            (inn if job_geo_status(j.get("location") or "", metro=metro, state=state) == "in" else rest).append(j)
        c = {"in": len(inn), "remote": 0, "unknown": len(rest), "out": 0}
        rows = inn + rest
    else:
        rows, c = partition_local(rows, lambda j: job_geo_status(j.get("location") or "", metro=metro, state=state))
    for j in rows[:c["in"]]:
        j["local"] = True
    st = state or US_METROS.get(metro, {}).get("state", "")
    return rows, {"metro": metro, "state": st, "label": scope_label(metro, state),
                  "state_label": US_STATES.get(st, ""), "counts": c, "source": "selector",
                  "statement": scope_statement("jobs", metro, state, c)}


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
    # EXPOSURE: the deeper the user has rotated (seen more), the deeper the candidate pool reaches —
    # otherwise rotation would just cycle inside the same top-400 forever.
    _seen_n = len(prefs.get("seen_ids") or [])
    cands = await store.match_jobs_scored(
        qvec, cap=int(prefs.get("candidate_cap", 400)) + min(3 * _seen_n, 800))
    cands = await widen_jobs_locally(store, cands, qvec=qvec, terms=(prefs.get("role_keywords") or None),
                                     country=str(prefs.get("country") or "us"),
                                     metro=str(prefs.get("metro") or ""), state=str(prefs.get("state") or ""),
                                     query_location="")
    # ROLE-KEYWORD pool widening: the semantic top-N may hold only a HANDFUL of jobs actually titled
    # what the user asked for (verified live: ~10 'payments' titles in a 430-job pool → rotation had
    # nothing fresh to promote). Pull a title-keyword slice of the WHOLE index per named role and
    # union it in at the pool's median similarity — the role-lead partition owns their placement,
    # rotation + preference scoring order them within it.
    _role_kw_early = [str(k).lower() for k in (prefs.get("role_keywords") or []) if str(k).strip()]
    if _role_kw_early and cands:
        _have = {(_norm_co(c.get("company") or ""), _norm_title(c.get("title") or "")) for c in cands}
        _sims = sorted(float(c.get("sim") or 0.0) for c in cands)
        _med = _sims[len(_sims) // 2]
        for _kw in _role_kw_early[:3]:
            try:
                _kw_rows = await store.search_jobs(terms=[_kw], cap=150)
            except Exception:   # noqa: BLE001 — widening is best-effort
                _kw_rows = []
            for _r in _kw_rows:
                _k2 = (_norm_co(_r.get("company") or ""), _norm_title(_r.get("title") or ""))
                if _k2 not in _have:
                    _have.add(_k2)
                    cands.append({**_r, "sim": _med})
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
    # VARIETY / EXPOSURE: ids the user has ALREADY been shown (client-remembered, capped). Seen jobs
    # are DEMOTED — never removed — so each run rotates fresh comparable options into the slate while
    # a still-clearly-best match can survive at the top. Deterministic quality, rotating exposure.
    seen = {str(x) for x in (prefs.get("seen_ids") or [])[:400] if str(x).strip()}
    dropped_country = 0
    # 3) score = semantic similarity + preference bonuses (with human-readable reasons)
    out = []
    # USER-LED exclusions: titles containing any excluded word are DROPPED outright ("roles I don't
    # even want") — the one hard filter, because the user explicitly asked to never see them.
    excl_kw = [str(k).strip().lower() for k in (prefs.get("exclude_keywords") or []) if str(k).strip()]
    dropped_excluded = 0
    for j in cands:
        title, loc, co = j.get("title") or "", (j.get("location") or "").lower(), (j.get("company") or "")
        if not _country_ok(loc, want_country):     # honor the country scope — drop clearly-foreign jobs
            dropped_country += 1; continue
        if excl_kw and any(k in title.lower() for k in excl_kw):
            dropped_excluded += 1; continue
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
        # Rotation identity = the JOB (company|title), not the row id: the same posting arrives as
        # multiple rows (second source, company-case variants) and the twin row must count as SEEN
        # too — otherwise run 2 re-serves what the user just saw under a fresh id.
        _key = _norm_co(co) + "|" + _norm_title(title)
        fresh = str(j.get("id") or "") not in seen and _key not in seen
        if not fresh:
            score -= 0.30       # already shown → fresh comparable matches lead this run
        out.append({**{k: j.get(k) for k in ("id", "company", "title", "location", "url", "source")},
                    "key": _key,
                    "score": round(score, 4), "match_pct": min(99, round(sim * 100)),
                    "seniority": jsen, "company_types": types, "reasons": reasons,
                    "fresh": fresh})
    out.sort(key=lambda x: -x["score"])
    # NEAR-DUPLICATE collapse: the same job often arrives via TWO sources (greenhouse + adzuna, with
    # company-case variants like 'Cloudflare'/'cloudflare') — burn ONE slot per (company, title),
    # keeping the best-scored row, so the slate spends its 40 slots on DISTINCT roles.
    _dedup: dict[tuple, dict] = {}
    for r_ in out:
        _k = (_norm_co(r_.get("company") or ""), _norm_title(r_.get("title") or ""))
        if _k not in _dedup:
            _dedup[_k] = r_
    out = list(_dedup.values())
    # USER-LED role priority: when the user NAMED role keywords, titles matching them LEAD the slate
    # (stable partition) — semantic look-alikes the user didn't ask for follow, they don't crowd out.
    if role_kw:
        out = ([r_ for r_ in out if any(k in (r_.get("title") or "").lower() for k in role_kw)]
               + [r_ for r_ in out if not any(k in (r_.get("title") or "").lower() for k in role_kw)])
    # COMPANY DIVERSITY (exposure): at most 3 rows per company lead the slate; the overflow is
    # appended after the diverse block — still available, never hidden — so one employer's hundred
    # postings can't crowd out the rest of the market.
    _per_co: dict[str, int] = {}
    _diverse, _overflow = [], []
    for r_ in out:
        _co = _norm_co(r_.get("company") or "")
        if _per_co.get(_co, 0) < 3:
            _per_co[_co] = _per_co.get(_co, 0) + 1
            _diverse.append(r_)
        else:
            _overflow.append(r_)
    out = _diverse + _overflow
    limit = int(prefs.get("limit", 40))
    slate = out[:limit]
    notes = []
    if want_country:
        notes.append(f"{want_country.upper()} only")
    if seen:
        _n_fresh = sum(1 for r_ in slate if r_.get("fresh"))
        notes.append(f"↻ rotating — {_n_fresh} of {len(slate)} are options you haven't seen; "
                     "previously shown matches rank lower, not gone")
    if dropped_excluded:
        notes.append(f"{dropped_excluded} excluded by your title filters")
    if prefs.get("min_salary"):
        notes.append("salary is best-effort — most listings don't publish pay")
    slate, _gs = apply_job_scope(slate, country=want_country, metro=str(prefs.get("metro") or ""),
                                 state=str(prefs.get("state") or ""))
    return {"jobs": slate, "geo_scope": _gs, "rotated": bool(seen),
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
                # underscores→spaces in CODE too (not just the prompt): role_keywords is substring-matched
                # against job titles, so a leaked 'staff_ml_engineer' must still become 'staff ml engineer'.
                "target_roles": [str(r).strip().lower().replace("_", " ") for r in (b.target_roles or []) if str(r).strip()][:6],
                "seniority": sen if sen in _SEN_OK else "",
                "search_text": (b.search_text or "").strip()}
    except Exception as e:   # noqa: BLE001 — never break matching; fall back to raw résumé
        _log.warning("build_candidate_brief failed: %s", e)
        return None


class _ApplyReq(BaseModel):
    requirement: str = ""      # a concrete requirement/skill the JD asks for
    evidence: str = ""         # the candidate's supporting experience FROM THE RÉSUMÉ ('' if none)
    verdict: str = "gap"       # strong | partial | gap


class _ApplyAnalysis(BaseModel):
    role_title: str = ""
    company: str = ""
    fit_score: int = 0                     # honest 0–100
    fit_summary: str = ""                  # one-line honest verdict
    requirements: list[_ApplyReq] = []     # the match table
    lead_with: list[str] = []              # real strengths to emphasize when applying
    gaps: list[str] = []                   # honest missing pieces (+ how to address)
    how_to_apply: list[str] = []           # tactical advice
    resume_tips: list[str] = []            # grounded edits (re-emphasize REAL experience; never invent)


def _resume_text_of(profile: dict, resume_text: str) -> str:
    t = (resume_text or "").strip() or str(profile.get("_resume_text") or "")
    if not t:
        wh = profile.get("work_history") if isinstance(profile.get("work_history"), list) else []
        t = (str(profile.get("summary", "")) + "\n" +
             "\n".join(f"{w.get('title','')} @ {w.get('company','')}: {w.get('description','')}"
                       for w in wh if isinstance(w, dict)))
    return t.strip()


async def build_apply_analysis(jd_text: str, profile: dict, resume_text: str, llm) -> dict | None:
    """Grounded APPLY analysis: read the JD + the candidate's résumé and lay out an honest fit table
    (each JD requirement × the candidate's real evidence × a verdict), what to lead with, honest gaps,
    how to apply best, and résumé tuning that ONLY re-surfaces REAL experience (never fabricates).
    LLM-owned (Rule 18); grounded in the two documents. None on any error/insufficient input."""
    jd = (jd_text or "").strip()
    rt = _resume_text_of(profile, resume_text)
    if len(jd) < 40 or len(rt) < 40 or llm is None:
        return None
    try:
        comp = await llm.complete(
            system=(
                "You are an expert career coach and technical recruiter helping THIS candidate decide how "
                "to apply to THIS role. Read the JOB DESCRIPTION and the CANDIDATE RÉSUMÉ and produce an "
                "HONEST, GROUNDED fit analysis so the candidate can focus on what matters. For each key "
                "requirement in the JD, set `evidence` to the candidate's supporting experience DRAWN FROM "
                "THE RÉSUMÉ (paraphrase real roles/achievements) and a `verdict`: 'strong' (clear match), "
                "'partial' (adjacent/some), or 'gap' (not shown). NEVER fabricate — if the résumé doesn't "
                "show it, verdict='gap' and evidence=''. fit_score: an honest 0–100. lead_with: the real "
                "strengths to emphasize. gaps: honest missing pieces and how to address them. how_to_apply: "
                "tactical advice for THIS application. resume_tips: concrete edits that ONLY re-emphasize, "
                "reorder, or surface the candidate's REAL experience for this role — never invent anything. "
                "Ground every statement in the two documents."),
            messages=[{"role": "user", "content": f"JOB DESCRIPTION:\n{jd[:9000]}\n\nCANDIDATE RÉSUMÉ:\n{rt[:9000]}"}],
            response_format=_ApplyAnalysis, max_tokens=4000)
        a = comp.parsed
        reqs = []
        for r in (a.requirements or []):
            if not r.requirement:
                continue
            v = r.verdict if r.verdict in ("strong", "partial", "gap") else "gap"
            # CODE-enforce the grounding invariant: a 'gap' carries NO evidence, even if the model
            # attached some — so the "no fabricated evidence" promise doesn't rest on the prompt alone.
            reqs.append({"requirement": r.requirement, "evidence": ("" if v == "gap" else r.evidence), "verdict": v})
        reqs = reqs[:14]
        return {"role_title": a.role_title, "company": a.company,
                "fit_score": max(0, min(100, int(a.fit_score or 0))), "fit_summary": a.fit_summary,
                "requirements": reqs, "lead_with": a.lead_with[:6], "gaps": a.gaps[:6],
                "how_to_apply": a.how_to_apply[:6], "resume_tips": a.resume_tips[:8]}
    except Exception as e:   # noqa: BLE001
        _log.warning("build_apply_analysis failed: %s", e)
        return None


class _CoverLetter(BaseModel):
    cover_letter: str = ""


async def build_cover_letter(jd_text: str, profile: dict, resume_text: str, llm, note: str = "") -> str | None:
    """Draft a concise, GENUINE cover letter — grounded in the résumé, nothing fabricated. Generated only
    on explicit candidate request. Returns the letter text (None on error/insufficient input)."""
    jd = (jd_text or "").strip()
    rt = _resume_text_of(profile, resume_text)
    if len(jd) < 40 or len(rt) < 40 or llm is None:
        return None
    name = str(profile.get("full_name") or profile.get("name") or "").strip()
    try:
        comp = await llm.complete(
            system=(
                "Write a concise, genuine cover letter (~250–320 words, first person, professional but warm) "
                "for THIS candidate applying to THIS role, in the `cover_letter` field. Ground EVERY claim in "
                "the candidate's résumé — nothing fabricated or exaggerated. Connect their real experience to "
                "the role's actual needs, express sincere interest, keep it specific (no generic filler). If a "
                "candidate note is provided, weave it in. No bracketed placeholders."),
            messages=[{"role": "user", "content":
                       f"CANDIDATE NAME: {name}\nCANDIDATE NOTE: {note[:500]}\n\nJOB DESCRIPTION:\n{jd[:9000]}"
                       f"\n\nCANDIDATE RÉSUMÉ:\n{rt[:9000]}"}],
            response_format=_CoverLetter, max_tokens=900)
        return (comp.parsed.cover_letter or "").strip() or None
    except Exception as e:   # noqa: BLE001
        _log.warning("build_cover_letter failed: %s", e)
        return None


class _ResumeRole(BaseModel):
    company: str = ""
    title: str = ""
    dates: str = ""            # verbatim from the original (e.g. "2021–Present")
    location: str = ""
    bullets: list[str] = []    # impactful, role-aligned bullets — grounded in this role's real work


class _ResumeSection(BaseModel):
    heading: str = ""          # any extra original section (Projects, Publications, Certifications, …)
    items: list[str] = []


class _ResumeDoc(BaseModel):
    """STRUCTURED résumé → rendered through a fixed template so formatting is consistent regardless of how
    messy the parsed source was. The LLM improves wording within these fields; hard facts stay grounded."""
    name: str = ""
    contact: str = ""          # one line: email · phone · location · links (only what's in the original)
    summary: str = ""          # compelling, role-aligned professional summary (grounded)
    experience: list[_ResumeRole] = []
    skills: list[str] = []
    education: list[str] = []
    extra: list[_ResumeSection] = []


def _render_resume_md(doc: "_ResumeDoc") -> str:
    """Render the structured résumé to CLEAN, CONSISTENT Markdown (controlled template → reliable PDF)."""
    out = []
    if doc.name:
        out.append(f"# {doc.name}")
    if doc.contact:
        out.append(doc.contact)
    if doc.summary:
        out += ["", "## Summary", doc.summary]
    if doc.experience:
        out += ["", "## Experience"]
        for r in doc.experience:
            hdr = " — ".join(x for x in [r.company, r.title] if x)
            meta = " | ".join(x for x in [r.dates, r.location] if x)
            out.append(f"**{hdr}**" + (f"  ·  {meta}" if meta else ""))
            out += [f"- {b}" for b in (r.bullets or []) if b]
            out.append("")
    if doc.skills:
        out += ["## Skills", ", ".join(doc.skills)]
    if doc.education:
        out += ["", "## Education"] + [f"- {e}" for e in doc.education if e]
    for s in (doc.extra or []):
        if s.heading and s.items:
            out += ["", f"## {s.heading}"] + [f"- {i}" for i in s.items if i]
    return "\n".join(out).strip()


async def build_tailored_resume(jd_text: str, profile: dict, resume_text: str, llm) -> str | None:
    """Produce a genuinely improved, role-tailored résumé — as STRUCTURED data rendered through a fixed
    template (consistent formatting), then a SURGICAL fact-check. The wording is strengthened and
    reprioritized for the role; hard facts (employers/titles/dates/degrees/named tech/metrics) stay
    grounded in the original. Returns clean Markdown (None on error/insufficient input)."""
    jd = (jd_text or "").strip()
    rt = _resume_text_of(profile, resume_text)
    if len(jd) < 40 or len(rt) < 40 or llm is None:
        return None
    try:
        comp = await llm.complete(
            system=(
                "You are an expert résumé writer tailoring THIS candidate's résumé to THIS role. Fill the "
                "structured fields from the ORIGINAL résumé. Make it GENUINELY BETTER, not a copy: write a "
                "compelling role-aligned summary; rewrite each experience bullet with a strong action verb + "
                "concrete impact; ORDER roles and bullets so the most role-relevant come first; mirror the "
                "job's terminology where it truthfully fits; drop filler. "
                "GROUNDING (hard rule): keep the SAME employers, titles, and date ranges as the original "
                "(verbatim dates), and the same education. You MAY sharpen and rephrase how real work is "
                "described, but you may NOT invent or add any employer, title, degree, named technology/tool, "
                "certification, numeric metric, OR any capability/responsibility/system (what they "
                "built/owned/led). If the role wants something the original doesn't show, DO NOT add it — a "
                "gap stays a gap. Improve only the WRITING of the candidate's real accomplishments. "
                "Include every real role and every real section (put non-standard sections in `extra`)."),
            messages=[{"role": "user", "content": f"TARGET ROLE (JOB DESCRIPTION):\n{jd[:9000]}\n\n"
                       f"ORIGINAL RÉSUMÉ (source of truth for all hard facts):\n{rt[:12000]}"}],
            response_format=_ResumeDoc, max_tokens=4000)
        doc = comp.parsed
        # SURGICAL fact-check: fix ONLY invented hard facts; PRESERVE all the improved wording/ordering.
        try:
            audit = await llm.complete(
                system=(
                    "Reconcile this rewritten structured résumé against the ORIGINAL so it is impactful AND "
                    "perfectly faithful. Do BOTH: "
                    "(1) RE-ADD anything real the rewrite dropped — every concrete fact in the original must "
                    "survive: each numeric metric (e.g. '40% latency', '1000s of GPUs'), every named "
                    "technology/tool (e.g. CUDA, Kubernetes), employer, title, date, degree, and each "
                    "distinct achievement. If the draft lost one, put it back (in the relevant bullet). "
                    "(2) REMOVE anything invented — any technology, tool, metric, capability, responsibility, "
                    "or system the original does NOT state (even if the target role wants it). "
                    "Keep the draft's stronger verbs, tighter wording, role-aligned emphasis, and ordering — "
                    "improve the WRITING, but the SET OF FACTS must exactly equal the original's. Return the "
                    "corrected structured résumé."),
                messages=[{"role": "user", "content":
                           f"ORIGINAL (ground truth):\n{rt[:12000]}\n\nSTRUCTURED DRAFT (JSON):\n{doc.model_dump_json()[:12000]}"}],
                response_format=_ResumeDoc, max_tokens=4000)
            doc = audit.parsed or doc
        except Exception:   # noqa: BLE001 — keep the (grounded-by-prompt) draft if the audit call fails
            pass
        md = _render_resume_md(doc)
        return md or None
    except Exception as e:   # noqa: BLE001
        _log.warning("build_tailored_resume failed: %s", e)
        return None


def _attr_display(f: dict) -> str:
    """Human display for a facet: the stored display value, except when the ingester stored a
    provenance NOTE there ('From metro' for a metro-derived country) — then the normalized value
    (e.g. 'US') is what a reader should see in a card or the Evidence Rail."""
    disp = (f.get("display_value") or "").strip()
    if disp.lower().startswith("from ") or not disp:
        val = (f.get("value_norm") or f.get("facet_value_norm") or "").strip()
        if val:
            return val.upper() if f.get("facet_key") == "country" else val.replace("_", " ")
    return disp


_IDENTITY_LINK_KINDS = {"github", "x", "twitter", "linkedin", "in", "email"}


def _dedupe_links(links: list[dict]) -> list[dict]:
    """One chip per IDENTITY profile kind (github/x/linkedin/email — a person has one of each; twin
    facet rows from separate ingest runs showed 'github github', '𝕏 𝕏'); other kinds (site/medium)
    dedupe by normalized URL so slash/case/www variants collapse but genuinely different sites stay."""
    seen_kind, seen_url, out = set(), set(), []
    for l in links:
        kind = (l.get("kind") or "").lower()
        u = (l.get("url") or "").strip().lower().rstrip("/")
        for pre in ("https://", "http://", "www."):
            u = u.removeprefix(pre) if u.startswith(pre) else u
        if kind in _IDENTITY_LINK_KINDS:
            if kind in seen_kind:
                continue
            seen_kind.add(kind)
        elif u in seen_url:
            continue
        seen_url.add(u)
        out.append(l)
    return out


def _dedupe_attrs(attrs: list[dict]) -> list[dict]:
    """Collapse duplicate attribute chips: same (key, normalized display), and metro ALIASES from
    divergent ingest normalizations ('san_francisco' + 'Bay Area' = one place, one chip)."""
    from roster_vertical.people_facets import METRO_ALIAS
    seen, out = set(), []
    for a in attrs:
        k = a.get("key") or ""
        norm = (a.get("display") or "").strip().lower().replace(" ", "_")
        if k == "metro":
            norm = METRO_ALIAS.get(norm, norm)
        sig = (k, norm)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(a)
    return out


def _person_countries(facet_rows: list[dict]) -> set[str]:
    """Best-effort countries for a person under a geo scope: explicit country facets first; when
    none, a known METRO implies its country (vertical map) — so metro=berlin with no country facet
    counts as KNOWN-foreign under a 'us' scope. Empty set = truly unknown (kept, recall)."""
    from roster_vertical.people_facets import METRO_COUNTRY
    out = {(f.get("value_norm") or f.get("facet_value_norm") or "").strip()
           for f in facet_rows if f.get("facet_key") == "country"}
    out.discard("")
    if out:
        return out
    for f in facet_rows:
        if f.get("facet_key") == "metro":
            c = METRO_COUNTRY.get((f.get("value_norm") or f.get("facet_value_norm") or "").strip())
            if c:
                out.add(c)
    return out


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
            attrs.append({"key": f["facet_key"], "display": _attr_display(f),
                          "document_id": f["document_id"], "block_id": f["block_id"]})
    links, attrs = _dedupe_links(links), _dedupe_attrs(attrs)
    from api.evidence import evidence_packet
    return {"entity_id": r["entity_id"], "name": r["name"], "blurb": _person_blurb(attrs),
            "attributes": attrs, "links": links, "citation": cite,
            "evidence": evidence_packet(facets, r["entity_id"])}


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
    cand_ids = [c["entity_id"] for c in cands]
    # LOCAL RECALL (recruiter's metro / state, no explicit locations): the JD cohort is drawn before
    # the scope applies, so add the best-matching people we can PLACE locally on the same scale
    _pm = str(prefs.get("metro") or "").lower(); _ps = str(prefs.get("state") or "").lower()
    if (_pm or _ps) and not [l for l in (prefs.get("locations") or []) if str(l).strip()] \
            and (prefs.get("country") or "us").lower() == "us":
        try:
            local_ids = await store.people_by_geo(metro=_pm, state=_ps)
            extra = [i for i in local_ids if i not in sim_map]
            if extra:
                top_local = await store.semantic_people(qvec, candidate_ids=extra, cap=250)
                if top_local:
                    sim_map.update(await store.similarity_for(qvec, top_local))
                    cand_ids += top_local
        except Exception as ex:  # noqa: BLE001 — additive
            _log.info("candidate local recall skipped: %s", ex)
    rows = await store.people_by_ids(cand_ids)
    want_sens = {str(s).lower() for s in (prefs.get("seniorities") or []) if str(s).strip()}
    want_country = (prefs.get("country") or "").lower()
    locs = [str(l).lower() for l in (prefs.get("locations") or []) if str(l).strip()]
    # LOCAL scope (the recruiter's metro / state) applies when no explicit locations were given:
    # clearly-elsewhere candidates dropped, unknown kept, confirmed-local lead (partition below)
    _lm = (str(prefs.get("metro") or "").lower() if not locs and want_country in ("", "us") else "")
    _ls = (str(prefs.get("state") or "").lower() if not locs and want_country in ("", "us") else "")
    from api.geo import partition_local, person_geo_status, scope_label, scope_statement
    _geo_dropped = 0
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
        if (_lm or _ls) and person_geo_status(facets, metro=_lm, state=_ls) == "out":
            _geo_dropped += 1
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
    _gs = None
    if _lm or _ls:
        _fac = {r["entity_id"]: r["facets"] for r in rows}
        out, _gc = partition_local(out, lambda p: person_geo_status(_fac.get(p["entity_id"], []), metro=_lm, state=_ls))
        _gc["out"] = _geo_dropped
        _st = _ls or US_METROS.get(_lm, {}).get("state", "")
        _gs = {"metro": _lm, "state": _st, "label": scope_label(_lm, _ls), "state_label": US_STATES.get(_st, ""),
               "counts": _gc, "source": "selector", "statement": scope_statement("people", _lm, _ls, _gc)}
    out = out[: int(prefs.get("limit", 40))]
    from api.artifacts import attach_artifacts
    await attach_artifacts(store, out)          # public artifacts + freshness on the returned cards
    return {"people_rows": out, "geo_scope": _gs,
            "note": (want_country.upper() + " only" if want_country else ""),
            "excluded_source_company": ({"companies": excl_raw, "count": excluded_n} if excl else None)}


async def parse_people_facets(question: str, llm) -> tuple[dict[str, list[str]], str, str]:
    facets, person, ctx, _terms = await parse_people_facets_full(question, llm)
    return facets, person, ctx


async def parse_people_facets_full(question: str, llm) -> tuple[dict[str, list[str]], str, str, list[str]]:
    """LLM query-compiler: free-text people question → (facet filter, person, person_context).
    `facets` is a normalized enumeration filter (empty when the question is not enumeration); `person`
    is a single named individual (identity/profile question) with `person_context` disambiguating
    hints. All empty = not a people query. Fail safe on any LLM/parse failure (never guess)."""
    from roster_vertical.people_facets import PEOPLE_FACET_KEYS, facet_parse_prompt
    try:
        comp = await llm.complete(
            system="You compile a people-search question into normalized facets, OR identify a single "
                   "named person. Return only the structured object; empty if it is not about people. "
                   "Also fill topic_terms: 3-6 short lowercase phrases naming the SPECIFIC domain or "
                   "subject the brief requires, including synonyms and adjacent terms a profile might "
                   "use (e.g. 'ad serving' -> ['ad serving','adtech','advertising','programmatic',"
                   "'real-time bidding']); leave it empty when the brief only names a role, level or "
                   "place with no specific subject.",
            messages=[{"role": "user", "content": facet_parse_prompt(question)}],
            response_format=_FacetParse, max_tokens=500)
        p = comp.parsed
    except Exception as e:  # noqa: BLE001 — a parse/provider failure must not crash the route
        _log.warning("parse_people_facets failed: %s", e)
        return {}, "", "", []
    out: dict[str, list[str]] = {}
    for k in PEOPLE_FACET_KEYS:
        vals = [str(v).strip().lower().replace(" ", "_")
                for v in (getattr(p, k, None) or []) if str(v).strip()]
        if vals:
            out[k] = vals
    terms = [str(t).strip().lower() for t in (getattr(p, "topic_terms", None) or []) if str(t).strip()][:6]
    return (out, (getattr(p, "person", "") or "").strip(), (getattr(p, "person_context", "") or "").strip(),
            terms)


_GENERIC_FUNCTIONS = {"engineering", "software_engineering", "backend", "frontend", "infrastructure",
                      "machine_learning", "data", "product", "design", "devops", "security", "research",
                      "management", "business", "sales", "marketing", "operations", "finance"}


def topic_terms_for(facets: dict, llm_terms: list[str] | None) -> list[str]:
    """The brief's SUBJECT terms: the compiler's topic_terms when it gave any, else code-derived from
    the compiled facets — every skill value and any non-generic function value, de-underscored — so
    anchoring never depends on the model remembering to fill the field."""
    terms = [t for t in (llm_terms or []) if t]
    if terms:
        return terms[:6]
    out: list[str] = []
    for v in (facets or {}).get("skill") or []:
        t = str(v).replace("_", " ").strip().lower()
        if len(t) >= 3 and t not in out:
            out.append(t)
    for v in (facets or {}).get("function") or []:
        if str(v) not in _GENERIC_FUNCTIONS:
            t = str(v).replace("_", " ").strip().lower()
            if len(t) >= 3 and t not in out:
                out.append(t)
    return out[:6]


def topic_hit(row: dict, terms: list[str]) -> str:
    """The first topic term that appears in the row's grounded text (attributes + blurb + artifact
    titles), else ''. Code-owned; whole-word-ish substring match on normalized text."""
    if not terms:
        return ""
    parts = [row.get("blurb") or ""] + [str(a.get("display") or "") for a in row.get("attributes") or []]
    parts += [str(it.get("title") or "") for it in ((row.get("artifacts") or {}).get("items") or [])]
    hay = " " + re.sub(r"[^a-z0-9]+", " ", " ".join(parts).lower()) + " "
    for t in terms:
        tt = " " + re.sub(r"[^a-z0-9]+", " ", t.lower()).strip() + " "
        if tt.strip() and tt in hay:
            return t
    return ""


def topic_partition(rows: list[dict], terms: list[str]) -> tuple[list[dict], int]:
    """STABLE partition: rows whose grounded text mentions the brief's topic lead (each order kept);
    marks `topic_hit` on rows. Returns (rows, n_anchored)."""
    if not terms or not rows:
        return rows, 0
    lead, rest = [], []
    for r in rows:
        h = topic_hit(r, terms)
        if h:
            r["topic_hit"] = h; lead.append(r)
        else:
            rest.append(r)
    return lead + rest, len(lead)


async def parse_people_refinement(question: str, prior_facets: dict, llm) -> dict[str, list[str]]:
    """REFINEMENT compile (conversation turns 2+): the LLM applies the utterance to the running
    filter — narrowing, expansion, removal, replacement, per the user's lead — and returns the FULL
    updated filter. {} = the utterance isn't about this people search (or the parse failed; the
    caller falls back to an additive merge)."""
    import json as _json
    from roster_vertical.people_facets import PEOPLE_FACET_KEYS, facet_refine_prompt
    try:
        comp = await llm.complete(
            system="You update an ongoing people-search filter per the user's refinement. "
                   "Return only the structured object.",
            messages=[{"role": "user", "content": facet_refine_prompt(
                question, _json.dumps(prior_facets, sort_keys=True))}],
            response_format=_FacetParse, max_tokens=400)
        p = comp.parsed
    except Exception as e:  # noqa: BLE001 — refinement-parse failure falls back to merge
        _log.warning("parse_people_refinement failed: %s", e)
        return {}
    out: dict[str, list[str]] = {}
    for k in PEOPLE_FACET_KEYS:
        vals = [str(v).strip().lower().replace(" ", "_")
                for v in (getattr(p, k, None) or []) if str(v).strip()]
        if vals:
            out[k] = vals
    return out


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
async def parse_job_refinement(question: str, prior_query: dict, llm) -> dict:
    """Jobs-tab REFINEMENT compile: apply the utterance to the running job query — narrow, expand,
    remove, or replace, per the user's lead — and return the FULL updated
    {company, title_keywords, location}. {} = not about this job search / parse failed."""
    import json as _json
    prompt = (
        "The user is refining an ONGOING job search. The CURRENT query (JSON) is:\n"
        + _json.dumps(prior_query, sort_keys=True) +
        "\n\nApply the user's utterance to that query and output the FULL UPDATED query as JSON with "
        "keys `company` (list of canonical lowercased employers), `title_keywords` (words that would "
        "appear IN a job title), `location` (city/region or 'remote' or empty):\n"
        "- narrowing ('only staff level', 'just at Stripe') → add/replace those fields, keep the rest;\n"
        "- expansion ('also Google', 'include frontend roles too') → append to that field;\n"
        "- removal ('anywhere', 'any company') → clear that field, keep the rest;\n"
        "- replacement ('in London instead') → replace that field;\n"
        "- a fresh unrelated job search → output the new search's query alone.\n"
        "Output an empty object ONLY if the utterance is clearly not about this job search.\n\n"
        "Utterance: " + question + "\nJSON:")
    try:
        comp = await llm.complete(
            system="You update an ongoing job-search query per the user's refinement. "
                   "Return only the structured object.",
            messages=[{"role": "user", "content": prompt}],
            response_format=_JobParse, max_tokens=300)
        p = comp.parsed
        out = {"company": [str(c).strip().lower().replace(" ", "_") for c in (p.company or [])][:6],
               "title_keywords": [str(t).strip().lower() for t in (p.title_keywords or [])][:8],
               "location": (p.location or "").strip()}
        return out if (out["company"] or out["title_keywords"] or out["location"]) else {}
    except Exception as e:  # noqa: BLE001
        _log.warning("parse_job_refinement failed: %s", e)
        return {}


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


_ENTITY_ID_RE = re.compile(r"^(github|openalex|theorg|npi|yc|sec|ef|aifund|sosv|pear|person):\S+$", re.I)
# a PICKED candidate as the UI submits it: "<name> — <distinguisher> [<entity id>]" (readable turn label)
_PICK_RE = re.compile(r"\[((?:github|openalex|theorg|npi|yc|sec|ef|aifund|sosv|pear|person):[^\]\s]+)\]\s*$", re.I)


def picked_entity_id(question: str) -> str:
    """The entity id a People-mode utterance PICKS: a bare id, or a trailing '[id]'; else ''."""
    q = (question or "").strip()
    if _ENTITY_ID_RE.match(q):
        return q
    m = _PICK_RE.search(q)
    return m.group(1) if m else ""
_CTX_STOP = {"the", "one", "who", "works", "worked", "work", "from", "with", "and", "for", "that",
             "this", "guy", "person", "at", "in", "of", "is", "was", "a", "an", "his", "her", "their",
             "based", "located", "university", "college", "company", "school", "studied", "went"}


def _row_text(row: dict) -> str:
    """Everything grounded we hold on a row, lowercased — the haystack for context matching."""
    parts = [row.get("name") or "", row.get("entity_id") or "", row.get("blurb") or ""]
    parts += [str(a.get("display") or "") for a in row.get("attributes") or []]
    parts += [str(l.get("url") or "") for l in row.get("links") or []]
    for a in ((row.get("artifacts") or {}).get("affiliations") or []):
        parts.append(str(a.get("name") or ""))
    return " ".join(parts).lower()


def resolve_candidates(rows: list[dict], ctx: str = "") -> tuple[str, list[dict]]:
    """CODE-OWNED identity resolution among same-named index rows.

    Returns (resolution, rows): 'none' (no candidates), 'resolved' (exactly one clear match), or
    'ambiguous' (several remain — the caller asks a clarifying question). Context tokens (company,
    school, location, role…) score against each row's grounded text; a single top scorer with any
    hit resolves. Never merges rows: two people who share a name stay two people."""
    rows = list(rows or [])
    if not rows:
        return "none", []
    if len(rows) == 1:
        return "resolved", rows
    toks = [t for t in re.findall(r"[a-z0-9][a-z0-9.+\-]{1,}", (ctx or "").lower())
            if t not in _CTX_STOP and len(t) >= 2]
    if not toks:
        return "ambiguous", rows
    scored = []
    for r in rows:
        hay = _row_text(r)
        scored.append((sum(1 for t in toks if t in hay), r))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    if top <= 0:
        return "ambiguous", rows
    leaders = [r for s, r in scored if s == top]
    if len(leaders) == 1:
        return "resolved", leaders
    return "ambiguous", leaders


def _distinguisher(row: dict) -> str:
    """A one-line, grounded 'which one' label for a candidate: company · role · place · source."""
    attrs = row.get("attributes") or []
    def g(k):
        v = next((a.get("display") for a in attrs if a.get("key") == k and a.get("display")), "")
        v = str(v).replace("_", " ")
        return v.title() if v == v.lower() else v      # 'ai_researcher' → 'Ai Researcher', keep 'Bay Area'
    src = (row.get("entity_id") or "").split(":", 1)[0]
    from api.evidence import FAMILY_LABELS
    bits = [g("company"), g("role") or g("seniority"), g("metro") or g("country")]
    bits.append("via " + (FAMILY_LABELS.get(src, src) if src else "index"))
    return " · ".join(b for b in bits if b)


def _clarify_text(name: str, rows: list[dict], resolution: str) -> str:
    if resolution == "none":
        return (f"“{name}” is not in Roster's people index yet — not evidence of absence. Add a "
                f"company, school, or location if you meant someone specific, or open a "
                f"web-grounded dossier in Q&A.")
    if resolution == "ambiguous":
        cos = [c for c in (next((a.get("display") for a in (r.get("attributes") or [])
                                 if a.get("key") == "company" and a.get("display")), "") for r in rows) if c]
        hint = f" (e.g. “{name} at {cos[0]}”)" if cos else ""
        return (f"Which {name}? {len(rows)} people in the index share this name. Pick one below, "
                f"or add a company, school, or location{hint}.")
    return ""


async def lookup_person(store, name: str, ctx: str = "", *, tenant_id: str = "demo") -> dict:
    """PERSON LOOKUP in People mode: bring up everything the index holds on a named person, on
    demand — or ask which one. kind='person' payload with `resolution` ∈ resolved|ambiguous|none,
    the matching rows (full cards with evidence packets + linked artifacts; the resolved person gets
    an on-demand artifact scan), a code-built clarifying question when needed, and the explicit
    profile-search links (secondary navigation aids, never the answer)."""
    from api.artifacts import attach_artifacts, scan_person_now
    name = " ".join((name or "").split())
    ctx = (ctx or "").strip()
    rows: list[dict] = []
    try:
        if _ENTITY_ID_RE.match(name):                       # a picked candidate (entity id) → direct
            raw = await store.people_by_ids([name], tenant_id=tenant_id)
        else:
            raw = await store.people_by_name(name, tenant_id=tenant_id, limit=12)
        rows = [_person_row_from_facets(r) for r in raw]
    except Exception as e:  # noqa: BLE001 — index trouble → honest 'none', never a crash
        _log.warning("lookup_person(%s) index read failed: %s", name, e)
        rows = []
    resolution, rows = resolve_candidates(rows, ctx)
    if resolution == "resolved":
        try:
            from api.artifacts import scan_person_extras
            pool = await store._get_pool()
            await scan_person_now(pool, rows[0]["entity_id"])   # on demand: papers/repos/orgs now
            _fr = next((r["facets"] for r in raw if r["entity_id"] == rows[0]["entity_id"]), [])
            await scan_person_extras(pool, rows[0]["entity_id"], _fr, rows[0],
                                     talks=(os.environ.get("ROSTER_TALKS_ENRICH", "1") == "1"))
        except Exception:  # noqa: BLE001
            pass
        # on demand: resolve their LinkedIn from search snippets (never reads linkedin.com) when the
        # index holds no LinkedIn link yet; a confident match is stored as self-stated facets
        if (os.environ.get("ROSTER_LINKEDIN_RESOLVE", "1") == "1"
                and not any((l.get("kind") or "") == "linkedin" for l in rows[0].get("links") or [])):
            try:
                import asyncio
                from api.linkedin_resolve import persist_resolution, resolve_linkedin
                dec = await asyncio.wait_for(resolve_linkedin(rows[0]), 12.0)
                if dec.get("status") == "resolved" and dec.get("match"):
                    await persist_resolution(store, rows[0]["entity_id"], dec["match"], tenant_id=tenant_id)
                    raw = await store.people_by_ids([rows[0]["entity_id"]], tenant_id=tenant_id)
                    if raw:
                        rows = [_person_row_from_facets(raw[0])]
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                _log.info("linkedin resolve skipped for %s: %s", rows[0].get("entity_id"), e)
    await attach_artifacts(store, rows)
    display_name = rows[0]["name"] if (resolution == "resolved" and rows) else (
        name if not _ENTITY_ID_RE.match(name) else (rows[0]["name"] if rows else name))
    for r in rows:
        r["distinguisher"] = _distinguisher(r)
    return {"kind": "person", "not_people_query": False,
            "person_card": build_person_profile_card(display_name, ctx),
            "people_rows": rows,
            "person_lookup": {"resolution": resolution, "name": display_name, "context": ctx,
                              "clarify": _clarify_text(display_name, rows, resolution),
                              "candidates": [{"entity_id": r["entity_id"], "name": r["name"],
                                              "label": r["distinguisher"]} for r in rows]}}


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


_INSIGHT_FILTER_KEYS = ("role", "seniority", "function", "industry", "metro", "company",
                        "worked_at", "country", "state", "stage", "accelerator", "skill")


class _AnalyticsSpec(BaseModel):
    """Compiled analytics question → a safe GROUP-BY spec. The LLM fills this; CODE runs the aggregation
    and owns the numbers (Rule 18). `target='abstain'` when the question can't be answered from facets."""
    target: str = ""                 # people | jobs | abstain
    group_by: str = ""               # people facet_key OR jobs col (company/location/source/department)
    metric: str = "distinct_people"  # people: distinct_people ; jobs: job_count
    top_n: int = 15
    unanswerable_reason: str = ""
    # filter facets (flat, like _FacetParse) — AND across keys, OR within a key:
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


async def parse_analytical_query(question: str, llm) -> _AnalyticsSpec | None:
    """LLM query-COMPILER (Rule 18) → a structured analytics spec. Fail-safe: returns an abstain spec on
    any error, never guesses. Keys/dimensions are validated against the allowlist by the caller/store."""
    if llm is None or len((question or "").strip()) < 3:
        return None
    try:
        comp = await llm.complete(
            system=(
                "Compile this analytics question about Roster's PEOPLE + JOBS index into a spec. "
                "target: 'people' (counts/distributions of professionals), 'jobs' (counts of open roles), "
                "or 'abstain' if it can't be answered from the index's facets (e.g. salary, tenure, growth "
                "over time, sentiment, anything time-series). "
                "group_by = the dimension to break down by — for people ONE of "
                "[role, seniority, function, industry, metro, company, worked_at, country, state, stage, "
                "accelerator, skill]; for jobs ONE of [company, location, source, department]. Prefer "
                "role/seniority over job titles. Map 'works at / at X' → company filter; 'worked at / "
                "ex-X / former' → worked_at. metric = 'distinct_people' (people) or 'job_count' (jobs). "
                "Fill the filter facet lists with NORMALIZED values (lowercase, snake_case) that constrain "
                "the population. top_n 5–25. If unanswerable, set target='abstain' and a short "
                "unanswerable_reason. Never invent facets or values."),
            messages=[{"role": "user", "content": (question or "")[:1000]}],
            response_format=_AnalyticsSpec, max_tokens=400)
        return comp.parsed
    except Exception as e:   # noqa: BLE001
        _log.warning("parse_analytical_query failed: %s", e)
        return _AnalyticsSpec(target="abstain", unanswerable_reason="Couldn't interpret the question.")


async def answer_roster_insights(*, question: str, tenant_id: str, store, llm) -> dict:
    """Grounded INSIGHTS Q&A over Roster's own people/jobs index: compile → coded GROUP-BY aggregation →
    grounded narration of ONLY the computed numbers, with honest coverage + data caveats + abstain.
    Never fabricates a statistic (the numbers come from the store, not the model)."""
    spec = await parse_analytical_query(question, llm)
    if spec is None or spec.target == "abstain" or spec.target not in ("people", "jobs") or not spec.group_by:
        reason = (getattr(spec, "unanswerable_reason", "") or
                  "I can answer questions about counts and distributions of people and jobs in Roster's "
                  "index (e.g. top companies hiring a role, skill or seniority breakdowns, where a role is "
                  "concentrated) — but not this one.")
        return {"grounded": False, "abstain": True, "answer": reason, "rows": [], "coverage_basis": None}
    top_n = max(5, min(int(spec.top_n or 15), 25))
    filters = {k: getattr(spec, k) for k in _INSIGHT_FILTER_KEYS if getattr(spec, k)}
    caveats = []
    if spec.target == "people":
        rows = await store.aggregate_people_facets(group_by=spec.group_by, filters=filters,
                                                   top_n=top_n, tenant_id=tenant_id)
        stats = await store.people_index_stats(tenant_id=tenant_id)
        fc = stats.get("facet_coverage", {}) or {}
        denom = fc.get(spec.group_by, 0)
        cov = _coverage_basis(filters, stats, sum(r["n"] for r in rows))
        cov["group_by"] = spec.group_by
        cov["group_facet_coverage"] = denom
        # DATA-QUALITY caveats (panel): sparse skill, former-employer, canonical basis.
        sparse = {"skill", "worked_at", "industry", "stage", "accelerator"}
        if spec.group_by in sparse or (set(filters) & sparse):
            caveats.append(f"'{spec.group_by if spec.group_by in sparse else 'skill'}' is a sparsely-tagged "
                           f"facet (only a fraction of profiles carry it), so this ranks within the tagged "
                           f"subset, not the whole index.")
        if spec.group_by == "worked_at" or "worked_at" in filters:
            caveats.append("worked_at = PAST employers (not current).")
        subject = "people"
    else:
        rows = await store.aggregate_jobs(group_by=spec.group_by,
                                          filters={"company": spec.company, "location": (spec.metro or spec.state or [None])[0],
                                                   "terms": (spec.role or spec.function or [])}, top_n=top_n)
        jstats = await store.jobs_stats()
        cov = {"jobs_indexed": jstats.get("jobs", 0), "companies_indexed": jstats.get("companies", 0),
               "group_by": spec.group_by, "not_ingested": NOT_INGESTED,
               "population_statement": (f"Counts over the {jstats.get('jobs', 0)} open roles currently in "
                                        f"Roster's job index (aggregated public ATS postings) — not every job "
                                        f"on the market.")}
        subject = "open roles"
    if not rows:
        return {"grounded": False, "abstain": False, "rows": [], "coverage_basis": cov,
                "answer": (f"No {subject} in Roster's index match that yet — it may be a sparsely-covered "
                           f"dimension. {cov.get('population_statement','')}")}
    # DETERMINISTIC grounded lead (numbers are code-computed, never model-authored).
    label = {"distinct_people": "people", "job_count": "open roles"}.get(spec.metric, subject)
    lines = [f"{i}. {r['display']} — {r['n']:,} {label}" for i, r in enumerate(rows, 1)]
    table = "\n".join(lines)
    narrative = ""
    try:
        comp = await llm.complete(
            system=("Write ONE short paragraph (2–4 sentences) summarizing this ranking QUALITATIVELY. "
                    "Do NOT include ANY numbers, counts, totals, sums, averages, or percentages — the exact "
                    "figures are shown separately in a chart. Describe the PATTERN in words only: which "
                    "entries lead, how concentrated or spread the distribution is, notable names or gaps. "
                    "Neutral and factual, no hype. If caveats are given, reflect them honestly."),
            messages=[{"role": "user", "content": json.dumps({
                "question": question[:400], "group_by": spec.group_by, "metric": spec.metric,
                "rows": rows[:15], "caveats": caveats})}],
            response_format=_Narrative, max_tokens=300)
        narrative = (comp.parsed.text or "").strip()
    except Exception:   # noqa: BLE001 — the deterministic table stands on its own if narration fails
        narrative = ""
    answer = "\n".join([p for p in [narrative, table] if p]
                       + ([""] + [f"⚠ {c}" for c in caveats] if caveats else [])
                       + ["", cov.get("population_statement", "")])
    return {"grounded": True, "abstain": False, "answer": answer.strip(), "narrative": narrative,
            "rows": rows, "coverage_basis": cov, "group_by": spec.group_by, "metric": spec.metric,
            "target": spec.target, "caveats": caveats}


class _Narrative(BaseModel):
    text: str = ""


async def answer_people_population(*, question: str, tenant_id: str, store, llm,
                                   scope_country: str = "",
                                   prior_facets: dict | None = None,
                                   assume_people: bool = False,
                                   prior_person: str = "",
                                   scope_metro: str = "", scope_state: str = "") -> dict:
    """Answer a people-enumeration question from the grounded people index. Always returns a structured
    result (never raises to the route): a compiled facet filter, grounded rows, and honest coverage.

    `scope_country` (from the top-right selector, flag-gated) HARD-filters results to that country — a
    `country=<scope>` facet is ANDed in, so people we cannot place there are excluded. A country the
    query itself names (compiler-parsed) OVERRIDES the selector default.

    `prior_facets` (conversation REFINEMENT): the accumulated filter from the previous turn in this
    People-tab conversation. The new utterance's parsed facets MERGE onto it — a new key NARROWS the
    previous result set, a repeated key REPLACES that dimension ("in Munich instead") — so follow-ups
    build on the results so far instead of starting fresh. Keys are sanitized against the vertical's
    facet vocabulary; New search (FE) clears the context."""
    _prior: dict = {}
    if prior_facets:
        from roster_vertical.people_facets import PEOPLE_FACET_KEYS
        _ok = set(PEOPLE_FACET_KEYS) | {"country", "state", "metro"}
        _prior = {k: [str(v).strip().lower().replace(" ", "_") for v in vals if str(v).strip()]
                  for k, vals in prior_facets.items()
                  if k in _ok and isinstance(vals, (list, tuple))}
        _prior = {k: v[:6] for k, v in _prior.items() if v}
    topic_terms: list[str] = []
    _q = (question or "").strip()
    _picked = picked_entity_id(_q)
    if _picked:
        # a PICKED candidate from a clarifying question (entity id) — no compile, direct lookup
        return await lookup_person(store, _picked, "", tenant_id=tenant_id)
    if prior_person and not _prior:
        # PERSON-LOOKUP CONVERSATION: the previous turn asked "which <name>?" (or found none). This
        # utterance is disambiguating CONTEXT for that name ("the one at Anthropic", "IIT Delhi")
        # unless it names a DIFFERENT person — then that person is looked up instead.
        f2, p2, c2 = await parse_people_facets(question, llm)
        if p2 and p2.strip().lower() != prior_person.strip().lower():
            return await lookup_person(store, p2, c2, tenant_id=tenant_id)
        return await lookup_person(store, prior_person, " ".join(x for x in [_q, c2] if x),
                                   tenant_id=tenant_id)
    if _prior:
        # REFINEMENT turn: the model applies the utterance to the RUNNING filter — narrow, expand,
        # remove, or replace, following the user's lead (Rule 18) — and returns the full new filter.
        facets, person, ctx = await parse_people_refinement(question, _prior, llm), "", ""
        if not facets:
            # fail-safe: plain parse + additive merge (an unparseable utterance never loses the set)
            f2, _p2, _c2 = await parse_people_facets(question, llm)
            facets = {**_prior, **f2} if f2 else {}
    else:
        facets, person, ctx, topic_terms = await parse_people_facets_full(question, llm)
        topic_terms = topic_terms_for(facets, topic_terms)
    if not facets and person and not _prior:
        # SINGLE-PERSON identity/profile lookup — everything the index holds on them, on demand
        # (full card, evidence, linked artifacts), or a clarifying question when several people
        # share the name, or an honest 'not indexed' with explicit profile-search links. kind='person'.
        return await lookup_person(store, person, ctx, tenant_id=tenant_id)
    stats = await store.people_index_stats(tenant_id=tenant_id)
    _forced_vibe = False
    if not facets:
        # BIAS TO CARDS: when the ROUTER asserted this seeks people (`assume_people`) but nothing
        # compiled into facets ("example people profiles that suit above roles"), serve it as a
        # pure SEMANTIC search over the (context-enriched) question text instead of dead-ending to
        # research prose. Without that assertion: not a people query — fall through as before.
        _forced_vibe = assume_people and (semantic_enabled() or people_semantic_first_enabled())
        if not _forced_vibe:
            return {"kind": "none", "grounded": False, "not_people_query": True, "people_rows": [],
                    "coverage_basis": None, "answer": ""}

    # GEO SCOPE (flag-gated): inject the selector country UNLESS the query already named one (query wins).
    if scope_country and not facets.get("country"):
        facets["country"] = [scope_country]
    # LOCAL SCOPE (the user's metro / state from the selector): applies only when the query itself
    # names no place — a query-named metro/state/country always wins. Clearly-elsewhere people are
    # dropped; unknown-location people are kept (they may be local); confirmed-local lead (below).
    from api.geo import person_geo_status, partition_local, scope_label, scope_statement
    _query_named_place = bool(facets.get("metro") or facets.get("state") or
                              (facets.get("country") and facets.get("country") != [scope_country]))
    _local_metro = (scope_metro or "").strip().lower() if not _query_named_place else ""
    _local_state = (scope_state or "").strip().lower() if not _query_named_place else ""
    _local = bool(_local_metro or _local_state)
    _local_dropped = 0

    _worked_at_union_used = False

    async def _enum_recall(fs: dict, cap: int = 200):
        """AND-enumerate with two RECALL-PRESERVING rules:
        1. Country never hard-gates: an entity with NO country facet is KEPT unless known-foreign
           (semantic-first's rule) — requiring the country KEY excluded almost every match
           (session b5353056). A country/geo-only filter stays strict (never 'everyone').
        2. worked_at unions in current-company matches: past-employer tagging is SPARSE in the
           index (worked_at=apple → 1 person; company=apple → 1,363) and 'worked at X' includes
           people still there — tagged alumni lead, current-X people follow, disclosed in
           coverage. Skipped when the query names company separately (ex-X now-at-Y stays exact)."""
        nonlocal _worked_at_union_used
        want = set(fs.get("country") or [])
        rest = {k: v for k, v in fs.items() if k != "country"}
        if not rest:
            return await store.enumerate_by_facets(fs, tenant_id=tenant_id, cap=cap)
        variants = [rest]
        if rest.get("worked_at") and not rest.get("company"):
            v2 = {k: v for k, v in rest.items() if k != "worked_at"}
            v2["company"] = rest["worked_at"]
            variants.append(v2)
        rows_, seen = [], set()
        for i, fs2 in enumerate(variants):
            for r in await store.enumerate_by_facets(fs2, tenant_id=tenant_id, cap=max(cap, 1000)):
                if r["entity_id"] not in seen:
                    seen.add(r["entity_id"])
                    rows_.append(r)
                    if i > 0:
                        _worked_at_union_used = True
        if want:
            kept = []
            for r in rows_:
                cv = _person_countries(r["facets"])   # explicit country, else metro→country
                if not cv or (cv & want):   # truly-unknown keeps the person (recall)
                    kept.append(r)
            # confirmed-scope people lead; unknown-country follow (stable within groups)
            kept.sort(key=lambda r: 0 if (_person_countries(r["facets"]) & want) else 1)
            rows_ = kept
        return rows_[:cap]

    # DEEP SEMANTIC SEARCH (flag ROSTER_SEMANTIC): filter by ALL facets (attributes), THEN rank by
    # OpenAI-embedding similarity — the eigen/noesis typed-block pattern (attributes filter, meaning
    # ranks). qvec None (flag off / no key / embed failure) → the exact facet path (byte-identical).
    qvec = embed_query(question) if (semantic_enabled() or people_semantic_first_enabled()) else None
    real_facets = [k for k in facets if k not in ("country", "state", "metro")]
    # IDENTITY facets (company / worked_at) are what the question is ABOUT — they must GATE, never
    # soften to a +0.03 boost. Semantic-first over the whole index let 164 OpenAlex researchers
    # dominate "people who worked at Apple" (the index is ~53% academics); with an identity facet
    # present we route to the HYBRID path below: hard facet filter first, semantic rank within it.
    _identity_facets = [k for k in ("company", "worked_at") if facets.get(k)]
    semantic_used = False
    semantic_first = False
    sf_sim: dict = {}                            # entity_id -> similarity (semantic-first → match_pct)
    if qvec and people_semantic_first_enabled() and not _identity_facets:
        # SEMANTIC-FIRST (flag): meaning leads. Rank ALL people by query→profile similarity; the ONLY
        # hard filter is country (drop just the known-foreign — an unknown country keeps the person, so
        # a sparse facet never strangles recall). Every parsed facet (skill/function/role/metro/…) is a
        # SOFT boost, not a gate — the fix for a query compressing into a sparse hard facet.
        scored = await store.match_people_scored(qvec, cap=500)
        sf_sim = {c["entity_id"]: c["sim"] for c in scored}
        cand_ids = [c["entity_id"] for c in scored]
        if topic_terms:
            # TOPIC ANCHOR: people whose profile TEXT mentions the topic are candidates even when the
            # embedding top-500 misses them (a sparse subject is drowned by role look-alikes otherwise)
            try:
                anchored_ids = await store.people_by_text(topic_terms, tenant_id=tenant_id, limit=300)
                extra = [i for i in anchored_ids if i not in sf_sim]
                if extra:
                    # same similarity scale as the global top-N (an un-scored extra would sort last)
                    sf_sim.update(await store.similarity_for(qvec, extra))
                    cand_ids += extra
            except Exception as ex:  # noqa: BLE001 — anchoring is additive
                _log.info("topic anchor skipped: %s", ex)
        if _local:
            # LOCAL RECALL: the global top-N is drawn before the scope applies, so confirmed-local people
            # are rare in it (7 of 200 in a Bay Area test). Add the best-matching people we can PLACE
            # in the metro/state, scored on the same similarity scale; the local partition leads with them.
            try:
                local_ids = await store.people_by_geo(metro=_local_metro, state=_local_state, tenant_id=tenant_id)
                local_extra = [i for i in local_ids if i not in sf_sim]
                if local_extra:
                    top_local = await store.semantic_people(qvec, candidate_ids=local_extra, cap=250)
                    if top_local:
                        sf_sim.update(await store.similarity_for(qvec, top_local))
                        cand_ids += top_local
            except Exception as ex:  # noqa: BLE001 — local recall is additive
                _log.info("local recall skipped: %s", ex)
        cand = await store.people_by_ids(cand_ids, tenant_id=tenant_id)
        want_country = set(facets.get("country") or [])
        boost = {k: set(v) for k, v in facets.items() if k != "country"}     # soft-boost facets
        ranked = []
        for r in cand:
            if want_country:
                cvals = _person_countries(r["facets"])   # explicit country, else metro→country
                if cvals and not (cvals & want_country):
                    continue                     # KNOWN-foreign → drop; unknown country → keep (recall)
            if _local and person_geo_status(r["facets"], metro=_local_metro, state=_local_state) == "out":
                _local_dropped += 1
                continue                         # LOCAL scope: clearly elsewhere → drop; unknown → keep
            rf = {(f["facet_key"], f["value_norm"]) for f in r["facets"]}
            nb = sum(1 for k, vals in boost.items() for v in vals if (k, v) in rf)
            r["_sf"] = sf_sim.get(r["entity_id"], 0.0) + 0.03 * min(nb, 4)   # similarity + soft boost
            ranked.append(r)
        ranked.sort(key=lambda r: -r["_sf"])
        rows = ranked[:200]
        if _local:
            # keep every confirmed-local candidate through the cut (they may sit below the global top-N)
            _loc_set = {r["entity_id"] for r in ranked[200:]
                        if person_geo_status(r["facets"], metro=_local_metro, state=_local_state) == "in"}
            rows += [r for r in ranked[200:] if r["entity_id"] in _loc_set][:250]
        if topic_terms:
            # keep EVERY topic-anchored candidate (they sit below the global top-N by construction —
            # their similarity is lower — but they are the people who actually mention the subject);
            # the topic partition later leads with them in their own relevance order
            _anch = {i for i in cand_ids if i not in {c["entity_id"] for c in scored}}
            rows += [r for r in ranked[200:] if r["entity_id"] in _anch][:300]
        semantic_used = semantic_first = bool(rows)
    elif qvec and real_facets:                   # HYBRID: attribute-filter → semantic-rank within it
        cand = await _enum_recall(facets, cap=1000)
        if _local:
            _n0 = len(cand)
            cand = [r for r in cand if person_geo_status(r["facets"], metro=_local_metro, state=_local_state) != "out"]
            _local_dropped += _n0 - len(cand)
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
        rows = [r for r in cand if _geo_ok(r)
                and not (_local and person_geo_status(r["facets"], metro=_local_metro, state=_local_state) == "out")][:200]
        semantic_used = bool(rows)
    else:
        rows = await _enum_recall(facets, cap=400 if _local else 200)
        if _local:
            _n0 = len(rows)
            rows = [r for r in rows if person_geo_status(r["facets"], metro=_local_metro, state=_local_state) != "out"]
            _local_dropped += _n0 - len(rows)
            rows = rows[:200]

    # GRACEFUL PROGRESSIVE RELAXATION: an over-specific query ANDs to zero (e.g. "sales GTM leaders in
    # California" → state=ca matches no business person; "engineers content platform at netflix" →
    # function=content matches nobody). When the full filter is empty, relax in TIERS — drop the sparse
    # GEO narrowing FIRST (least semantic), then skill, then function/industry — keeping the meaning
    # (a sales/engineer intent) as long as possible, and never relaxing down to a geo/country-only
    # filter (which would return "everyone"). Returns the closest honest match + a note of what relaxed.
    _TIERS = [("industry", "metro", "state"), ("skill",), ("function",), ("role", "seniority")]
    _GEO_ONLY = {"country", "state", "metro"}   # never relax down to a geo/country-only filter
    # MULTI-COMPANY (talent-cluster) queries relax on LOW yield, not only zero — "top X at A and B"
    # compiles into a seniority AND-gate that starves the comparison (1 row total, empty clusters).
    # Seniority relaxes FIRST there ("top" is a ranking wish, not a hard filter); role+company stay.
    _multi_co = len([c for c in (facets.get("company") or []) if str(c).strip()]) > 1
    _relax_floor = 12 if _multi_co else 1
    _tiers = ([("seniority",), ("skill",), ("industry", "metro", "state"), ("function",)]
              if _multi_co else _TIERS)
    relaxed_from: list[str] = []
    if len(rows) < _relax_floor:
        kept, dropped = dict(facets), []
        best_rows, best_from, best_kept = rows, [], facets
        for tier in _tiers:
            drop_now = [k for k in tier if k in kept]
            if not drop_now:
                continue
            for k in drop_now:
                kept.pop(k, None); dropped.append(k)
            if not any(k not in _GEO_ONLY for k in kept):     # nothing meaningful left → stop
                break
            r2 = await _enum_recall(kept, cap=300 if _multi_co else 200)
            if len(r2) > len(best_rows):
                best_rows, best_from, best_kept = r2, list(dropped), dict(kept)
            if len(r2) >= _relax_floor:
                break
        if len(best_rows) > len(rows):
            rows, relaxed_from, facets = best_rows, best_from, best_kept

    coverage = _coverage_basis(facets, stats, len(rows))
    if _worked_at_union_used:
        coverage["population_statement"] = (coverage.get("population_statement", "") +
            " Past-employer (worked-at) tagging is sparse in the index, so results include people "
            "CURRENTLY at the named company as well as tagged alumni (alumni listed first).")
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
                attrs.append({"key": f["facet_key"], "display": _attr_display(f),
                              "document_id": f["document_id"], "block_id": f["block_id"]})
        links, attrs = _dedupe_links(links), _dedupe_attrs(attrs)
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
        from api.evidence import evidence_packet
        people_rows.append({
            "entity_id": r["entity_id"], "name": r["name"], "blurb": _person_blurb(attrs),
            "attributes": attrs, "links": links, "citation": cite,
            "evidence": evidence_packet(r["facets"], r["entity_id"])})

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

    if not semantic_used:            # facet path: the seniority ladder is the relevance signal
        people_rows.sort(key=lambda p: (-_score(p), p["name"]))
        for p in people_rows:
            p["relevance"] = round(min(1.0, _score(p) / 10.0), 3)

    # EVIDENCE-AWARE RANKING (evidence-model-v2 §4, default order): relevance BANDS (0.05) are the
    # primary key; within a band, code-computed evidence depth (corroborated/consistent affiliation,
    # artifact-backed capability, brief-matching artifacts, seniority-from-evidence when asked,
    # footprint, freshness, headline fit) orders the rows. Artifacts attach here so the read sees them.
    _ranked_by_evidence = False
    if people_rows:
        from api.artifacts import attach_artifacts
        from api.evidence import rank_read, rank_sort_key
        await attach_artifacts(store, people_rows)
        for p in people_rows:
            p["rank_read"] = rank_read(p, facets)
        people_rows.sort(key=rank_sort_key)
        _ranked_by_evidence = True

    # CONFIRMED-COUNTRY priority under a geo scope: people we can PLACE in the scoped country lead;
    # unknown-location profiles follow (recall keeps them; rank stops them crowding out confirmed).
    _want_c = set(facets.get("country") or [])
    if _want_c and people_rows:
        _placed = {r["entity_id"]: bool(_person_countries(r["facets"]) & _want_c) for r in rows}
        people_rows = ([p for p in people_rows if _placed.get(p["entity_id"])]
                       + [p for p in people_rows if not _placed.get(p["entity_id"])])

    # PRIMARY-ROLE priority: when the query names role(s), people whose FIRST (primary) role facet
    # IS that role lead the list; multi-tagged profiles (an SWE who also carries a data_scientist
    # tag) follow, each group keeping its existing order — the "data scientists ranked at the
    # bottom" fix (session 0cb80174). Stable partition: relevance order survives within groups.
    _want_roles = set(facets.get("role") or [])
    if _want_roles and people_rows:
        def _primary_role(p):
            v = next((a["display"] for a in p["attributes"] if a["key"] == "role"), "")
            return (v or "").strip().lower().replace(" ", "_")
        people_rows = ([p for p in people_rows if _primary_role(p) in _want_roles]
                       + [p for p in people_rows if _primary_role(p) not in _want_roles])

    # LOCAL-SCOPE partition: people we can PLACE in the user's metro/state lead; unknown-location
    # rows follow (kept for recall). Then the topic partition, so subject-mentioning locals lead.
    if _local and people_rows:
        _fac = {r["entity_id"]: r["facets"] for r in rows}
        people_rows, _gc = partition_local(
            people_rows, lambda p: person_geo_status(_fac.get(p["entity_id"], []), metro=_local_metro, state=_local_state))
        _gc["out"] = _local_dropped
        coverage["geo_scope"] = {"metro": _local_metro, "state": _local_state or US_METROS.get(_local_metro, {}).get("state", ""),
                                 "label": scope_label(_local_metro, _local_state),
                                 "state_label": US_STATES.get(_local_state or US_METROS.get(_local_metro, {}).get("state", ""), ""),
                                 "counts": _gc, "source": "selector",
                                 "statement": scope_statement("people", _local_metro, _local_state, _gc)}
        people_rows = people_rows[:200]          # the local recall widened the cohort; cut to the page budget

    # TOPIC ANCHOR partition (LAST, so it wins over the country / primary-role partitions): people
    # whose grounded text mentions the brief's subject lead; the coverage statement says how many the
    # index holds and that the rest are wording look-alikes.
    n_topic = 0
    if topic_terms and people_rows:
        people_rows, n_topic = topic_partition(people_rows, topic_terms)
        people_rows = people_rows[:200]
        for p in people_rows:
            if p.get("topic_hit") and isinstance(p.get("rank_read"), dict):
                p["rank_read"]["reasons"].insert(0, f"profile mentions the brief's subject ({p['topic_hit']})")
        coverage["topic_anchor"] = {"terms": topic_terms, "mentioning": n_topic}
        _t0 = topic_terms[0]
        if n_topic:
            coverage["population_statement"] = (
                f"{n_topic} of these people mention {_t0} (or a related term) in their grounded profile "
                f"text and are listed first. The rest are the closest matches by wording and may not "
                f"be {_t0}-related. " + coverage.get("population_statement", ""))
        else:
            coverage["population_statement"] = (
                f"Nobody in the index mentions {_t0} (or a related term: {', '.join(topic_terms[1:4])}) "
                f"— the rows below are the closest matches by wording, NOT {_t0} specialists. This is "
                f"an index coverage gap. " + coverage.get("population_statement", ""))

    # TALENT CLUSTERS: a query naming SEVERAL companies ("top infra talent at Anthropic and
    # OpenAI") is a STAFFING COMPARISON — group the results per company, each cluster with its
    # code-computed composition (headcount in index, seniority mix, function mix). Numbers come
    # from the rows, never the model; this is Roster's indexed view, not true headcount.
    _comp_vals = [_norm_co(c) for c in (facets.get("company") or []) if str(c).strip()]
    if len(_comp_vals) > 1 and people_rows:
        _by_co: dict[str, list[dict]] = {}
        for p in people_rows:
            _p_cos = {_norm_co(a.get("display") or "") for a in p["attributes"]
                      if a.get("key") == "company"}
            for c in _comp_vals:
                if c in _p_cos:
                    _by_co.setdefault(c, []).append(p)
                    break                     # a person clusters under the FIRST queried company hit
        clusters = []
        for c in _comp_vals:
            members = _by_co.get(c) or []
            if not members:
                clusters.append({"company": c, "count": 0, "entity_ids": [],
                                 "seniority_mix": [], "function_mix": []})
                continue
            _sen: dict[str, int] = {}
            _fn: dict[str, int] = {}
            for p in members:
                _seen_sen, _seen_fn = set(), set()
                for a in p["attributes"]:
                    # normalize display variants ('Senior'/'senior'/'senior ') so a person counts
                    # ONCE per distinct level/function in the mix
                    d = (a.get("display") or "").strip().replace("_", " ").title()
                    if not d:
                        continue
                    if a.get("key") == "seniority" and d not in _seen_sen:
                        _seen_sen.add(d); _sen[d] = _sen.get(d, 0) + 1
                    elif a.get("key") == "function" and d not in _seen_fn:
                        _seen_fn.add(d); _fn[d] = _fn.get(d, 0) + 1
            clusters.append({
                "company": c, "count": len(members),
                "entity_ids": [p["entity_id"] for p in members],
                "seniority_mix": sorted(_sen.items(), key=lambda kv: -kv[1])[:4],
                "function_mix": sorted(_fn.items(), key=lambda kv: -kv[1])[:4]})
        coverage["clusters"] = clusters

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

    # EVIDENCE DISTRIBUTION (spec: coverage is a first-class surface) — how many rows per headline
    # evidence state, counted by code from the per-row packets (rows are fully built here).
    if people_rows:
        # PUBLIC ARTIFACTS (evidence-model-v2 step 1) were attached before ranking; count the
        # distribution here (rows are final) and say how the order was produced.
        from api.artifacts import attach_artifacts, footprint_coverage
        from api.evidence import evidence_groups
        if not any("artifacts" in p for p in people_rows):
            await attach_artifacts(store, people_rows)
        coverage["footprint"] = footprint_coverage(people_rows)
        coverage["evidence_groups"] = evidence_groups(people_rows)
        coverage["ranking"] = ("how closely each profile's wording matches your brief first; among "
                               "near-equal matches, the strength of public evidence (confirmed employer, "
                               "linked papers and repos, recent activity, LinkedIn headline fit) — the "
                               "reasons for each person are under Inspect evidence")
    return {"grounded": grounded, "not_people_query": False, "answer": answer,
            "people_rows": people_rows, "coverage_basis": coverage}
