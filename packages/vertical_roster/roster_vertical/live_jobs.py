"""Live ATS job-discovery leg for Jobs Map (roster vertical).

Mirrors live_people.py but for JOBS: Exa neural search over the public ATS boards
(Greenhouse / Lever / Ashby) with category "job posting" → live open roles, normalized to
Roster's job-row shape and tagged as a live/unverified source (never grounded). Nothing here
writes to the corpus. The kernel `ExternalRecordSearch` port is reused unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from roster_kernel.providers.base import ProviderMode
from roster_kernel.providers.record_search import (
    CassetteRecordSearch,
    CompositeRecordSearch,
    ExternalRecord,
)

# Public ATS hosts whose postings Exa should surface (the "posted jobs on ATS boards" ask).
_ATS_DOMAINS = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
                "jobs.ashbyhq.com", "ashbyhq.com"]


def live_jobs_available() -> bool:
    """True when Exa has a key (the ATS-discovery provider). Absence is not an error — dormant."""
    return bool(os.environ.get("EXA_API_KEY"))


class ExaJobSearch:
    """Exa neural search restricted to ATS boards (category 'job posting') → ExternalRecord per
    posting. Fail-safe to [] with no key / any error (the leg is additive, never load-bearing)."""

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
        payload = {"query": q, "numResults": min(int(max_results), 100), "type": "auto",
                   "category": "job posting", "includeDomains": _ATS_DOMAINS,
                   "contents": {"text": {"maxCharacters": 1200}}}
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
            out.append(ExternalRecord(
                id=url, source="exa", title=str(r.get("title") or url),
                text=str(r.get("text") or ""), url=url,
                fields={"published": r.get("publishedDate"), "score": r.get("score")}))
        return out


def build_live_jobs_search(*, mode: ProviderMode | str | None = None,
                           cassette_root: Path | str = "", clients: list | None = None) -> CassetteRecordSearch:
    inner = None
    if clients is not None:
        inner = CompositeRecordSearch(clients)
    else:
        m = ProviderMode(mode) if isinstance(mode, str) else mode
        if m is not ProviderMode.REPLAY:
            inner = CompositeRecordSearch([ExaJobSearch()])
    return CassetteRecordSearch(inner, cassette_root=Path(cassette_root), namespace="jobs_live", mode=mode)


def _company_from_ats_url(url: str) -> str:
    """The board token in an ATS URL is a reliable company slug (…greenhouse.io/<co>/…,
    …lever.co/<co>/…, …ashbyhq.com/<co>/…)."""
    u = (url or "").lower()
    m = (re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9][\w-]*)", u)
         or re.search(r"lever\.co/([a-z0-9][\w-]*)", u)
         or re.search(r"ashbyhq\.com/([a-z0-9][\w-]*)", u))
    return m.group(1).replace("-", " ").replace("_", " ").strip() if m else ""


def _split_title_company(title: str) -> tuple[str, str]:
    """Pull (title, company) out of an ATS result title: 'Title at Company', 'Company - Title', etc."""
    t = (title or "").strip()
    for sep in (" at ", " @ "):
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip(), b.strip()
    for sep in (" - ", " – ", " | "):
        if sep in t:
            a, b = t.split(sep, 1)
            return b.strip(), a.strip()   # "Company - Title" → title is the second half
    return t, ""


def normalize_job_record(rec: ExternalRecord) -> dict | None:
    """Map one ATS posting record → Roster's job-row shape (company/title/location/url/source), with
    citation cleared and a live/source label so it's never read as a grounded, verified posting.
    Returns None when there's no usable title."""
    if rec is None:
        return None
    url = str(rec.url or "").strip()
    title, comp_from_title = _split_title_company(str(rec.title or ""))
    company = _company_from_ats_url(url) or comp_from_title
    # Drop board landing/listing pages (not an individual posting) — generic titles, or an ATS URL
    # with no per-job path segment.
    if not title or title.strip().lower() in ("jobs", "careers", "open roles", "job board",
                                              "openings", "current openings", "all jobs"):
        return None
    board = ("greenhouse" if "greenhouse" in url else "lever" if "lever" in url
             else "ashby" if "ashby" in url else "web")
    return {"id": f"live:exa:{url}", "company": (company or "").title()[:80], "title": title[:140],
            "location": "", "url": url, "source": board, "found_by": "live:exa",
            "live": True, "citation": None,
            "_live_fields": {"text": str(rec.text or ""), "company": company,
                             "score": float(rec.score or 0.0)}}
