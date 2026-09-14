"""Live people-search leg for Find-candidates (roster vertical).

The KERNEL owns the generic `ExternalRecordSearch` port + cassette discipline; this vertical
module supplies the domain: two concrete provider clients (People Data Labs, Exa) that return
`ExternalRecord`s, and a normalizer that maps a record into roster's people-card shape. Nothing
here writes to the corpus — Stage 1 is an EPHEMERAL, clearly-labelled leg blended at query time
(see apps/api/live_people.py). Durable ingestion of a shortlisted person is a later, async step.

Provider modes ride the same replay/record/live cassette as the web leg, so tests cost nothing;
the exact PDL/Exa request shapes are refined against real responses at record-time (Stage 2).
"""
from __future__ import annotations

import os
from pathlib import Path

from roster_kernel.providers.base import ProviderMode
from roster_kernel.providers.record_search import (
    CassetteRecordSearch,
    CompositeRecordSearch,
    ExternalRecord,
)

# The ONLY seniority values the matcher/UI understand (mirror people_population `_SEN` / FE SEN_OPTS).
_SEN = ("intern", "junior", "mid", "senior", "staff_plus", "leadership")


def live_people_available() -> bool:
    """True when at least one live provider has a key configured (used to decide whether to build
    the leg live vs replay-only). Absence of keys is not an error — the leg simply stays dormant."""
    return bool(os.environ.get("PDL_API_KEY") or os.environ.get("EXA_API_KEY"))


class PdlPeopleSearch:
    """People Data Labs Person Search → ExternalRecord per matched person. Lazy httpx; key from
    PDL_API_KEY. Fail-safe: no key or any error → [] (the leg is additive, never load-bearing)."""

    URL = "https://api.peopledatalabs.com/v5/person/search"

    def __init__(self, *, api_key: str | None = None, timeout: float = 12.0):
        self._api_key = api_key or os.environ.get("PDL_API_KEY", "")
        self._timeout = timeout

    def _build_es(self, query: str, filters: dict) -> dict:
        """Build a PDL Elasticsearch query from the brief filters. Titles/skills as `should` (soft),
        location/country as `filter` (hard) — mirrors the corpus leg's soft-skills / hard-geo split."""
        must: list[dict] = []
        should: list[dict] = []
        f = filters or {}
        for sk in (f.get("skills") or [])[:8]:
            if str(sk).strip():
                should.append({"match": {"skills": str(sk).strip()}})
        if str(f.get("search_text") or query).strip():
            should.append({"match": {"summary": str(f.get("search_text") or query).strip()[:500]}})
        for loc in (f.get("locations") or [])[:6]:
            if str(loc).strip():
                should.append({"match": {"location_names": str(loc).strip()}})
        if (f.get("country") or "").strip():
            must.append({"term": {"location_country": str(f["country"]).strip().lower()}})
        bool_q: dict = {}
        if must:
            bool_q["must"] = must
        if should:
            bool_q["should"] = should
            bool_q["minimum_should_match"] = 1
        return {"bool": bool_q or {"must": [{"match_all": {}}]}}

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        if not self._api_key:
            return []
        import httpx
        payload = {"query": {"query": self._build_es(query, filters or {})},
                   "size": min(int(max_results), 100)}
        headers = {"X-Api-Key": self._api_key, "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self.URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:   # noqa: BLE001 — additive leg; a failure contributes nothing
            return []
        out: list[ExternalRecord] = []
        for p in data.get("data", []) or []:
            pid = str(p.get("id") or p.get("linkedin_url") or p.get("full_name") or "").strip()
            if not pid:
                continue
            title = str(p.get("full_name") or "").strip()
            head = str(p.get("job_title") or "").strip()
            comp = str(p.get("job_company_name") or "").strip()
            text = " · ".join(x for x in (title, head, comp, str(p.get("summary") or "")) if x)
            out.append(ExternalRecord(
                id=pid, source="pdl",
                title=(f"{title} — {head}" if head else title),
                text=text or title,
                url=str(p.get("linkedin_url") or "").strip(),
                fields=p))
        return out


class ExaPeopleSearch:
    """Exa NEURAL people search using the `linkedin profile` category, which returns real profiles with
    a STRUCTURED `entities[].properties` block (name, location, workHistory → title + company) plus the
    profile text. A generic web search (the old call) threw that structure away and returned page-title
    'names' and no geo. Fail-safe to [] with no key / any error."""

    URL = "https://api.exa.ai/search"

    def __init__(self, *, api_key: str | None = None, timeout: float = 12.0):
        self._api_key = api_key or os.environ.get("EXA_API_KEY", "")
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        if not self._api_key:
            return []
        import httpx
        f = filters or {}
        q = str(f.get("search_text") or query).strip()
        locs = ", ".join(str(x) for x in (f.get("locations") or [])[:3] if str(x).strip())
        q = (q + (f" in {locs}" if locs else "")).strip()[:600]
        payload = {"query": q, "numResults": min(int(max_results), 25), "type": "neural",
                   "category": "linkedin profile",
                   "contents": {"text": {"maxCharacters": 900}}}
        headers = {"x-api-key": self._api_key, "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self.URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:   # noqa: BLE001 — additive leg
            return []
        out: list[ExternalRecord] = []
        for r in data.get("results", []) or []:
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            ents = r.get("entities") or []
            props = next((e.get("properties") for e in ents
                          if isinstance(e, dict) and e.get("type") == "person" and e.get("properties")), None)
            out.append(ExternalRecord(
                id=url, source="exa",
                title=str((props or {}).get("name") or r.get("title") or url),
                text=str(r.get("text") or ""), url=url,
                fields={"person": props, "title_raw": r.get("title"),
                        "published": r.get("publishedDate"), "score": r.get("score")}))
        return out


def build_live_people_search(*, mode: ProviderMode | str | None = None,
                             cassette_root: Path | str,
                             clients: list | None = None) -> CassetteRecordSearch:
    """Cassette-wrapped composite of the live providers. `clients` override is for tests (inject a
    FakeRecordSearch). In replay mode the inner may be None (the cassette answers)."""
    inner = None
    if clients is not None:
        inner = CompositeRecordSearch(clients)
    else:
        m = ProviderMode(mode) if isinstance(mode, str) else mode
        if m is not ProviderMode.REPLAY:
            inner = CompositeRecordSearch([PdlPeopleSearch(), ExaPeopleSearch()])
    return CassetteRecordSearch(inner, cassette_root=Path(cassette_root), namespace="people", mode=mode)


def _clean(s) -> str:
    return str(s or "").strip()


_US_STATE_WORDS = ("alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming")
_FOREIGN = {"india": "in", "canada": "ca", "united kingdom": "gb", "england": "gb", "scotland": "gb",
    "germany": "de", "france": "fr", "australia": "au", "singapore": "sg", "netherlands": "nl",
    "ireland": "ie", "spain": "es", "italy": "it", "brazil": "br", "china": "cn", "japan": "jp",
    "pakistan": "pk", "bangladesh": "bd", "nigeria": "ng", "israel": "il", "poland": "pl", "sweden": "se",
    "switzerland": "ch", "mexico": "mx", "united arab emirates": "ae", "iran": "ir", "argentina": "ar",
    "philippines": "ph", "indonesia": "id", "vietnam": "vn", "turkey": "tr", "türkiye": "tr", "egypt": "eg",
    "kenya": "ke", "south africa": "za", "ukraine": "ua", "romania": "ro", "portugal": "pt", "colombia": "co",
    "chile": "cl", "peru": "pe", "malaysia": "my", "thailand": "th", "saudi arabia": "sa", "qatar": "qa",
    "sri lanka": "lk", "nepal": "np", "belgium": "be", "denmark": "dk", "norway": "no", "finland": "fi",
    "austria": "at", "greece": "gr", "new zealand": "nz", "hong kong": "hk", "taiwan": "tw", "south korea": "kr"}
# Foreign metros/areas that name no country word (LinkedIn's "Greater <city> Area" style).
_FOREIGN_CITY = {"bengaluru": "in", "bangalore": "in", "mumbai": "in", "hyderabad": "in", "chennai": "in",
    "pune": "in", "kolkata": "in", "gurgaon": "in", "gurugram": "in", "noida": "in", "ahmedabad": "in",
    "tehran": "ir", "toronto": "ca", "vancouver": "ca", "montreal": "ca", "london": "gb", "manchester": "gb",
    "berlin": "de", "munich": "de", "paris": "fr", "amsterdam": "nl", "dublin": "ie", "sydney": "au",
    "melbourne": "au", "dubai": "ae", "abu dhabi": "ae", "lagos": "ng", "nairobi": "ke", "lahore": "pk",
    "karachi": "pk", "dhaka": "bd", "sao paulo": "br", "são paulo": "br", "buenos aires": "ar",
    "mexico city": "mx", "warsaw": "pl", "kyiv": "ua", "kiev": "ua", "istanbul": "tr", "cairo": "eg",
    "singapore": "sg", "tel aviv": "il", "beijing": "cn", "shanghai": "cn", "bangkok": "th"}


def _country_from_location(loc: str) -> str:
    """Best-effort country from a free-text location → 'us' / an ISO-ish code / '' (unknown → kept).
    US wins on 'united states'/'usa' or a US state name; a named foreign country maps to its code."""
    s = (loc or "").lower()
    if not s.strip():
        return ""
    if "united states" in s or "u.s." in s or s.endswith(", us") or ", usa" in s or s.endswith(" usa"):
        return "us"
    for name, code in _FOREIGN.items():
        if name in s:
            return code
    # a named US state outranks an ambiguous foreign city ("Manchester, New Hampshire" is US, not UK)
    if any(st in s for st in _US_STATE_WORDS):
        return "us"
    for city, code in _FOREIGN_CITY.items():
        if city in s:
            return code
    # common US metro/area phrases that name no state (LinkedIn's "… Bay Area" style)
    if any(m in s for m in ("bay area", "silicon valley", "greater seattle", "greater boston",
                            "greater new york", "greater los angeles", "greater chicago",
                            "greater houston", "greater austin", "greater denver", "greater atlanta",
                            "greater phoenix", "greater philadelphia", "greater minneapolis",
                            "greater sacramento", "greater san diego", "dallas", "austin", "denver",
                            "atlanta", "houston", "seattle", "portland", "miami", "washington dc",
                            "washington d.c.")):
        return "us"
    return ""


def normalize_record(rec: ExternalRecord) -> dict | None:
    """Map one ExternalRecord → a people-card partial in the shape match_jd_people/UI expect:
    {entity_id, name, blurb, attributes[], links[], citation:None, source, found_by, _live_fields}.

    Live rows carry citation=None and a `source`/`found_by` label so they are NEVER mistaken for a
    grounded corpus row. entity_id is a synthetic, stable `live:<source>:<id>` (dedup / future
    upsert keying). Returns None for a record with no usable name."""
    if rec is None:
        return None
    src = _clean(rec.source) or "live"
    f = rec.fields or {}
    if src == "pdl":
        name = _clean(f.get("full_name")) or _clean(rec.title)
        title = _clean(f.get("job_title"))
        company = _clean(f.get("job_company_name"))
        metro = _clean(f.get("location_locality")) or _clean(f.get("location_name"))
        country = _clean(f.get("location_country"))
        skills = [_clean(s) for s in (f.get("skills") or []) if _clean(s)][:12]
        li = _clean(f.get("linkedin_url")) or _clean(rec.url)
    else:  # exa linkedin profile — prefer the structured `person` entity Exa returns
        p = f.get("person") or {}
        name = _clean(p.get("name")) or _clean(rec.title).split(" | ")[0].split(" — ")[0][:80]
        loc = _clean(p.get("location"))
        wh = [w for w in (p.get("workHistory") or []) if isinstance(w, dict)]
        cur = wh[0] if wh else {}
        title = _clean(cur.get("title"))
        _co = cur.get("company")
        company = _clean(_co.get("name") if isinstance(_co, dict) else _co)
        metro = loc
        country = _country_from_location(loc)
        skills = []
        li = _clean(rec.url)
    if not name:
        return None
    attrs = []
    if title:
        attrs.append({"key": "title", "display": title})
    if company:
        attrs.append({"key": "company", "display": company})
    if metro:
        attrs.append({"key": "metro", "display": metro})
    if country:
        attrs.append({"key": "country", "display": country})
    for sk in skills[:8]:
        attrs.append({"key": "skill", "display": sk})
    links = []
    if li:
        kind = "linkedin" if "linkedin.com" in li.lower() else "web"
        links.append({"kind": kind, "url": li})
    blurb = " · ".join(x for x in (title, company, metro) if x) or _clean(rec.text)[:160]
    return {"entity_id": f"live:{src}:{rec.id}", "name": name, "blurb": blurb,
            "attributes": attrs, "links": links, "citation": None,
            "source": src, "found_by": f"live:{src}",
            "_live_fields": {"skills": skills, "title": title, "company": company,
                             "metro": metro, "country": country, "text": _clean(rec.text)}}
