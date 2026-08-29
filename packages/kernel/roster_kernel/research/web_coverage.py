"""On-demand WEB coverage fan-out (reflection pass, flag ROSTER_REFLECTION=steer).

The "muted: didn't look" fix. When a LANDSCAPE / multi-entity / general question needs evidence the
corpus does not hold (business dimensions — moat, ICP, distribution, differentiation — for specific
named startups), the coverage machinery must ACTIVELY collect from the web instead of reporting "the
evidence is thin." Today the coverage-leg fan-out (react.py) queries the CORPUS only, and the only
per-entity web machinery (deep readers, entity_open) fires solely for single-entity questions — so a
multi-entity landscape ask never web-searches its named players. This module closes that gap.

Domain-free MECHANIC only (kernel litmus): it takes a caller-built list of queries (the caller derives
WHAT to search from the vertical's contract entities/axes — meaning lives there, Rule 18), fires them as
BOUNDED, concurrent open-web searches through the supplied source, and returns normal BlockHits. The
caller screens them through the SAME web-quality + span-gate + liveness path as the existing web:deep
legs, so grounding is unchanged. Any error/timeout on a leg yields [] for that leg — a dead leg never
breaks the answer.
"""
from __future__ import annotations

import asyncio
import logging

from roster_kernel.contract.dto import BlockHit, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource

_log = logging.getLogger(__name__)

_DEFAULT_MAX_QUERIES = 8          # hard cap on web legs — credit/latency bound
_DEFAULT_MAX_RESULTS_PER_QUERY = 4   # (dialed back from a 12×6 widening that regressed latency; revisit)
_DEFAULT_MAX_CHARS = 6000
_DEFAULT_MAX_CHUNKS_PER_PAGE = 3
_DEFAULT_CONCURRENCY = 4          # gentler on the open-web provider (fewer simultaneous searches → fewer 429s)


async def retrieve_web_coverage(
    *,
    queries: list[str],
    source: RetrievalSource,
    tenant_id: str,
    workspace_id: str | None = None,
    max_queries: int = _DEFAULT_MAX_QUERIES,
    max_results_per_query: int = _DEFAULT_MAX_RESULTS_PER_QUERY,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_chunks_per_page: int = _DEFAULT_MAX_CHUNKS_PER_PAGE,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[BlockHit]:
    """Fire the (deduped, capped) queries as concurrent open-web searches; return combined BlockHits.

    web_open=True drops the trusted-domain whitelist so the players' own/business coverage is reachable;
    the caller MUST screen the result through the web-quality judge + span-gate (as the web:deep legs do)
    before it can be cited. Returns [] on no source / no queries / total failure.

    ROBUSTNESS (the thin-coverage fix): each leg RETRIES ONCE on an error or an empty result — the open-web
    provider throttles/fails transiently under load, and a whole fan-out silently collapsing to [] is worse
    than a slower one. A per-fan-out SUMMARY is logged (legs / ok / empty / failed / hits) so a degraded run
    is VISIBLE (Rule 13), never a silent shrug.
    """
    if source is None or not queries:
        return []
    # dedupe case-insensitively, drop blanks, cap
    seen: set[str] = set()
    qs: list[str] = []
    for q in queries:
        qn = (q or "").strip()
        k = qn.lower()
        if qn and k not in seen:
            seen.add(k)
            qs.append(qn)
        if len(qs) >= max(1, int(max_queries)):
            break
    if not qs:
        return []

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    stats = {"ok": 0, "empty": 0, "failed": 0}

    async def _search_once(q: str) -> list[BlockHit]:
        return await source.search(RetrievalRequest(
            query=q, tenant_id=tenant_id, workspace_id=workspace_id,
            k=max_results_per_query * max_chunks_per_page, web_open=True,
            web_max_results=max_results_per_query, web_max_chars=max_chars,
            web_max_chunks_per_page=max_chunks_per_page,
            web_extra_facets={"source_kind": "web", "web_role": "coverage"}))

    async def _one(q: str) -> list[BlockHit]:
        async with sem:
            for attempt in (1, 2):           # retry ONCE on error OR empty (transient provider throttling)
                try:
                    r = await _search_once(q)
                except Exception as e:   # noqa: BLE001 — a dead leg never breaks the answer
                    if attempt == 2:
                        stats["failed"] += 1
                        _log.warning("web-coverage leg failed (both attempts) on %r: %s", q, e)
                        return []
                    continue
                if r:
                    stats["ok"] += 1
                    return r
                if attempt == 2:             # two empties → genuinely nothing for this query
                    stats["empty"] += 1
                    return []
            return []

    results = await asyncio.gather(*(_one(q) for q in qs))
    out: list[BlockHit] = []
    for r in results:
        out.extend(r or [])
    _log.info("web-coverage fan-out: %d legs → ok=%d empty=%d failed=%d, %d hits",
              len(qs), stats["ok"], stats["empty"], stats["failed"], len(out))
    return out


def build_coverage_queries(entities: list[str], axes: list[str], *, cap: int = _DEFAULT_MAX_QUERIES,
                           topic: str = "") -> list[str]:
    """Structural expansion (Rule 18 — code owns the shape, the LLM-derived entities/axes/topic own the
    meaning): TOPIC-anchored axis legs FIRST (the business dimensions a thin corpus misses — 'moat',
    'ICP', 'distribution' — anchored to the question's subject so a bare 'moat' search isn't off-topic),
    THEN entity×axis legs (per named category/player), axis-major round-robin, capped. Deduped. `topic`
    is a short subject string from the caller (the question's core); when empty the axis legs are bare
    (today's behavior). Empty entities → axis-only; empty axes → entity-only; both empty → []."""
    axes = [a.strip() for a in (axes or []) if a and a.strip()]
    entities = [e.strip() for e in (entities or []) if e and e.strip()]
    topic = (topic or "").strip()
    seen: set[str] = set()
    out: list[str] = []

    def _add(q: str) -> bool:
        q = " ".join((q or "").split()).strip()     # collapse whitespace
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            out.append(q)
        return len(out) < cap

    for a in axes:                       # topic-anchored axis legs — the missing business dimensions
        if not _add(f"{topic} {a}" if topic else a):
            return out
    for a in (axes or [""]):             # then per-entity×axis (or bare entity when no axes)
        for e in entities:
            if not _add(f"{e} {a}"):
                return out
    return out
