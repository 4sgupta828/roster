"""Bounded additive deep web retrieval for one PERSON (parallel to deep_company.py).

Kernel mechanics only: expand caller-supplied NAME-based facet templates, run bounded web searches
(open + search-indexed public sources — the vertical decides which; LinkedIn/X are NOT scraped), return
normal span-gated BlockHits, and harvest the person's public PROFILE links (in the vertical's preference
order) as a byproduct. All vocabulary + sourcing policy live in the vertical (kernel litmus).

Unlike deep_company there is no single canonical "domain" for a person, so queries are name-based; the
person's own surfaces (github/personal site) are just facets in the template, not a resolved domain.
Any error/timeout -> ([], []) so the normal answer path is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any, Mapping
from urllib.parse import urlparse

from roster_kernel.contract.dto import BlockHit, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.research.deep_company import _render, _template_items, _host

_log = logging.getLogger(__name__)

_DEFAULT_MAX_QUERIES = 8
_DEFAULT_MAX_PAGES = 20
_DEFAULT_DEADLINE_S = 28.0
_DEFAULT_MAX_RESULTS = 3
_DEFAULT_MAX_CHARS = 12000
_DEFAULT_MAX_CHUNKS = 5
_PROFILE_RESULTS = 8


def _profile_link(url: str, preference: tuple[str, ...]) -> tuple[int, str] | None:
    """If `url` is a profile on a preferred host, return (pref_rank, normalized_url). Lower rank = better
    (LinkedIn < X < GitHub ...). Non-profile URLs → None. GitHub/LinkedIn/X profile URLs only (a path that
    looks like an individual handle, not a repo/company page)."""
    host = _host(url)
    for rank, pref in enumerate(preference):
        if host == pref or host.endswith("." + pref):
            path = urlparse(url).path.strip("/")
            first = path.split("/", 1)[0] if path else ""
            if not first:
                return None
            # github.com/{user} but not github.com/{org}/{repo}; linkedin.com/in/{slug}; x.com/{handle}
            if pref == "github.com" and "/" in path:
                return None
            if "linkedin.com" in pref and not path.startswith("in/"):
                return None
            return (rank, url.split("?", 1)[0])
    return None


async def retrieve_deep_person(
    *,
    person: str,
    templates: Mapping[str, Any],
    source: RetrievalSource,
    tenant_id: str,
    workspace_id: str | None = None,
    llm=None,
    budget=None,
) -> tuple[list[BlockHit], list[dict]]:
    """Return (additive span-grounded BlockHits, resolved profile links [{name,url,host}])."""

    async def _run() -> tuple[list[BlockHit], list[dict]]:
        name = (person or "").strip()
        if not name or source is None or not templates:
            return [], []
        max_queries = max(1, int(templates.get("max_queries") or _DEFAULT_MAX_QUERIES))
        max_pages = max(1, int(templates.get("max_pages") or _DEFAULT_MAX_PAGES))
        max_results = max(1, int(templates.get("max_results_per_query") or _DEFAULT_MAX_RESULTS))
        max_chars = max(1000, int(templates.get("max_chars") or _DEFAULT_MAX_CHARS))
        max_chunks = max(1, int(templates.get("max_chunks_per_page") or _DEFAULT_MAX_CHUNKS))
        concurrency = max(1, int(templates.get("concurrency") or 3))
        preference = tuple(templates.get("profile_preference") or ())

        # facet queries — NAME-based (no domain). internal = own surfaces (self-reported), external = press.
        specs: list[tuple[str, str, dict]] = []
        for facet, tmpl in _template_items(templates, "internal"):
            specs.append((facet, _render(tmpl, company=name, domain="").replace("{person}", name),
                          {"source_kind": "corp_eng", "web_role": "official"}))
        for facet, tmpl in _template_items(templates, "external"):
            specs.append((facet, _render(tmpl, company=name, domain="").replace("{person}", name),
                          {"source_kind": "news", "web_role": "independent_analysis"}))
        specs = specs[:max_queries]
        if not specs:
            return [], []

        sem = asyncio.Semaphore(concurrency)

        async def _fetch(facet: str, query: str, stamp: dict) -> list[BlockHit]:
            async with sem:
                return await source.search(RetrievalRequest(
                    query=query, tenant_id=tenant_id, workspace_id=workspace_id,
                    k=max_results * max_chunks, web_open=True, web_max_results=max_results,
                    web_max_chars=max_chars, web_max_chunks_per_page=max_chunks,
                    web_extra_facets={**stamp, "deep_facet": facet}))

        # extra probe to surface profile LINKS (linkedin/x/github)
        prof_q = _render(str(templates.get("profile_query_template") or "{person} profile"),
                         company=name, domain="").replace("{person}", name)
        prof_task = _fetch("profiles", prof_q, {"source_kind": "news", "web_role": "independent_analysis"})

        batches = await asyncio.gather(prof_task, *(_fetch(*s) for s in specs))
        prof_hits, facet_batches = batches[0], batches[1:]

        # harvest profile links (pref-ordered, deduped by host)
        cand: list[tuple[int, str]] = []
        for h in list(prof_hits) + [x for b in facet_batches for x in b]:
            pl = _profile_link(h.document_id, preference)
            if pl:
                cand.append(pl)
        cand.sort(key=lambda t: t[0])
        profiles: list[dict] = []
        seen_host: set[str] = set()
        for _rank, url in cand:
            host = _host(url)
            if host not in seen_host:
                seen_host.add(host); profiles.append({"name": person, "url": url, "host": host})

        # assemble additive hits (dedup pages, cap, stamp web:deep_person)
        out: list[BlockHit] = []
        seen_pages: set[str] = set()
        for b in facet_batches:
            for h in b:
                if h.document_id not in seen_pages:
                    if len(seen_pages) >= max_pages:
                        continue
                    seen_pages.add(h.document_id)
                out.append(replace(h, legs=tuple([*(getattr(h, "legs", ()) or ()), "web:deep_person"])))
        return out, profiles

    try:
        deadline = float(templates.get("deadline_s") or _DEFAULT_DEADLINE_S)
        return await asyncio.wait_for(_run(), timeout=max(1.0, deadline))
    except Exception as e:  # noqa: BLE001
        _log.warning("deep-person retrieval failed: %r", e)
        return [], []
