"""Ephemeral live ATS job-discovery leg for Jobs Map (app glue).

Fetch open roles from ATS boards (Greenhouse/Lever/Ashby) via Exa, score them against the query
vector, obey the geo scope, and return them in Roster's job-row shape — labeled live/unverified,
never written to the corpus. Behind ROSTER_LIVE_JOBS (default OFF → the corpus jobs path is
unchanged). Mirrors api/live_people.py.
"""
from __future__ import annotations

import os

from api.geo import job_geo_status
from api.live_people import _cosine, _norm_co, _parse_vec   # shared vector/text helpers
from api.people_population import calibrated_pct, embed_texts
from roster_vertical.people_facets import US_METROS, US_STATES

_log = __import__("logging").getLogger("roster.live_jobs")


def live_jobs_enabled() -> bool:
    """Flag (default OFF, Rule 20): discover live ATS jobs (Exa) for Jobs Map."""
    return os.environ.get("ROSTER_LIVE_JOBS", "").lower() in ("1", "true", "yes")


def _cap() -> int:
    try:
        return max(1, int(os.environ.get("ROSTER_LIVE_JOBS_CAP", "100")))
    except ValueError:
        return 100


def _weight() -> float:
    try:
        return max(0.1, min(1.0, float(os.environ.get("ROSTER_LIVE_JOBS_WEIGHT", "0.9"))))
    except ValueError:
        return 0.9


def build_live_jobs_search(*, cassette_root=None, clients=None):
    """Build the ATS job search — call Exa directly whenever a key exists (no jobs cassettes yet)."""
    from roster_vertical.live_jobs import build_live_jobs_search as _bld, live_jobs_available
    from roster_kernel.providers.base import resolve_mode
    if clients is not None or cassette_root:
        return _bld(mode=resolve_mode(), cassette_root=(cassette_root or ""), clients=clients)
    if not live_jobs_available():
        _log.warning("live jobs leg on but no EXA_API_KEY configured")
        return None
    from roster_kernel.providers.record_search import CompositeRecordSearch
    from roster_vertical.live_jobs import ExaJobSearch
    return CompositeRecordSearch([ExaJobSearch()])


async def merge_live_jobs(qvec: str, prefs: dict, *, search_client=None,
                          max_out: int | None = None, query_text: str | None = None) -> list[dict]:
    """Return live ATS job rows ranked by relevance to the query. Geo-scoped (metro/state/country),
    deduped by (company, title). Fail-safe: any error → []. No DB writes."""
    prefs = prefs or {}
    client = search_client if search_client is not None else build_live_jobs_search()
    if client is None:
        return []
    cap = int(max_out) if max_out else _cap()
    from roster_vertical.live_jobs import normalize_job_record

    search_text = str(query_text or prefs.get("search_text") or "")[:2000]
    filters = {"locations": [str(l) for l in (prefs.get("locations") or [])][:6], "search_text": search_text}
    query = search_text.strip()
    if len(query) < 4:
        _log.info("live jobs: no usable query text — skipping")
        return []
    try:
        records = await client.search(query, max_results=max(cap, 20), filters=filters)
    except Exception as ex:   # noqa: BLE001
        _log.warning("live jobs search failed: %s", ex)
        return []
    _log.info("live jobs: %d raw ATS records for %r", len(records or []), query[:60])

    rows = [r for r in (normalize_job_record(rec) for rec in (records or [])) if r]
    if not rows:
        return []

    # GEO scope: drop a posting clearly outside every chosen metro/state; unknown/remote kept (recall).
    _scopes: list[tuple[str, str]] = []
    for l in [str(x).lower().strip() for x in (prefs.get("locations") or []) if str(x).strip()]:
        if l in US_METROS:
            _scopes.append((l, ""))
        elif l in US_STATES:
            _scopes.append(("", l))
    if not _scopes:
        _m = str(prefs.get("metro") or "").lower(); _s = str(prefs.get("state") or "").lower()
        if _m in US_METROS or _s in US_STATES:
            _scopes.append((_m if _m in US_METROS else "", _s if _s in US_STATES else ""))

    seen: set[tuple[str, str]] = set()
    kept: list[dict] = []
    for r in rows:
        lf = r.get("_live_fields") or {}
        key = (_norm_co(r.get("company") or ""), (r.get("title") or "").lower().strip())
        if key in seen:                    # one row per (company, title)
            continue
        seen.add(key)
        if _scopes:
            _sts = [job_geo_status(lf.get("text") or "", metro=m, state=s) for m, s in _scopes]
            if _sts and all(x == "out" for x in _sts):
                continue
        kept.append(r)
    if not kept:
        return []

    q = _parse_vec(qvec)
    vecs = embed_texts([((r.get("_live_fields") or {}).get("text") or r.get("title") or "")[:8000] for r in kept])
    w = _weight()
    out: list[dict] = []
    for r, v in zip(kept, vecs):
        lf = r.get("_live_fields") or {}
        cos = _cosine(q, _parse_vec(v)) if v else 0.0
        sim = max(0.0, cos) * w if cos else float(lf.get("score") or 0.0) * 0.5   # Exa score as fallback rank
        r["match_pct"] = calibrated_pct(sim)
        r["reasons"] = [f"live · {r.get('source') or 'ats'}"]
        r["_score"] = sim
        out.append(r)
    out.sort(key=lambda x: -x["_score"])
    return out[:cap]
