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
    """Exa neural search for open-web profile pages → ExternalRecord per result. Reuses the kernel's
    Exa web client (its /search) rather than duplicating the HTTP. Fail-safe to [] with no key."""

    def __init__(self, *, api_key: str | None = None, timeout: float = 12.0):
        self._api_key = api_key or os.environ.get("EXA_API_KEY", "")
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        if not self._api_key:
            return []
        from roster_kernel.providers.exa_web import ExaWebSearch
        f = filters or {}
        # bias the neural query toward profiles for the role/skills the brief named
        q = str(f.get("search_text") or query).strip()
        locs = ", ".join(str(x) for x in (f.get("locations") or [])[:3] if str(x).strip())
        q = (q + (f" in {locs}" if locs else "")).strip()[:600]
        try:
            web = ExaWebSearch(api_key=self._api_key, timeout=self._timeout)
            results = await web.search(q, max_results=min(int(max_results), 25), open_web=True)
        except Exception:   # noqa: BLE001
            return []
        out: list[ExternalRecord] = []
        for r in results:
            if not r.url:
                continue
            out.append(ExternalRecord(
                id=r.url, source="exa", title=r.title or r.url,
                text=(r.body or r.snippet or r.title or ""), url=r.url,
                fields={"snippet": r.snippet, "highlights": list(r.highlights or ()),
                        "published": r.published}))
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
    else:  # exa / open-web
        name = _clean(rec.title).split(" — ")[0].split(" | ")[0][:80]
        title = company = metro = country = ""
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
