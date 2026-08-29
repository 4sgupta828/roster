"""S2c — LIVE 2nd-source fetchers (Exa news + EDGAR Form D) + the enrichment runner.

Wires the injected-fetcher pipeline (`multisource_enrich.enrich_company`, S2b) to the REAL
sources: Exa neural web search for funding/traction NEWS (attaches to the targeted company),
and EDGAR full-text Form D for private-raise FILINGS (whose issuer is run through the S2a
entity resolver — a real match attaches at the `primary_filing` tier, an SPV/namesake is
rejected). This is the only module that touches the live web/EDGAR; the pipeline + resolver
stay source-agnostic and offline-tested.
"""
from __future__ import annotations

import hashlib


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def _grounding_text(r, cap: int = 4500) -> str:
    """Assemble the grounding text for ONE Exa result: the QUERY-AWARE highlights FIRST (the most
    relevant sentences Exa already extracted — we pay for these, so use them), then the page body,
    capped. Putting highlights first guarantees the discriminating span survives the cap even when
    the body is long — the exact truncation-loss the highlights feature exists to prevent."""
    body = (getattr(r, "body", None) or getattr(r, "snippet", "") or "").strip()
    highlights = [h.strip() for h in (getattr(r, "highlights", None) or ()) if h and h.strip()]
    parts: list[str] = []
    if highlights:
        parts.append("\n".join(highlights))
    if body:
        parts.append(body)
    return ("\n\n".join(parts))[:cap]


# NEWS recency floor: a diligence graph must reflect the CURRENT round, so bias news to recent pages
# (Exa returns most-relevant, which lets a dense older overview out-rank this quarter's raise).
_NEWS_RECENCY_DAYS = 540


async def news_fetcher(company_name: str, *, web, max_results: int = 6) -> list[dict]:
    """Exa web search for funding/traction news about `company_name` → candidate NEWS docs
    (source_key='news' → `analysis` tier). Each doc carries highlights+body as grounding text, biased
    to RECENT pages (`_NEWS_RECENCY_DAYS`) and narrowed to the `news` category so press leads over
    marketing. Fail-safe → []. NEWS is not a filing, so the pipeline attaches it to the company."""
    query = (f"Latest funding rounds, investors, valuation, revenue and customer traction for "
             f"{company_name}")
    try:
        try:
            results = await web.search(query, max_results=max_results,
                                       recency_days=_NEWS_RECENCY_DAYS, category="news")
        except TypeError:
            # a web provider without the recency/category kwargs (e.g. the free fallback) → plain call
            results = await web.search(query, max_results=max_results)
    except Exception:   # noqa: BLE001
        return []
    docs: list[dict] = []
    seen: set = set()
    for r in (results or []):
        text = _grounding_text(r)
        url = getattr(r, "url", "") or ""
        if not text or url in seen:
            continue
        seen.add(url)
        docs.append({
            "source_key": "news", "document_id": "news:" + _sha(url or text),
            "text": text, "facets": {"source_kind": "news", "url": url,
                                     "published": getattr(r, "published", None) or ""},
        })
    return docs


_SITE_FACETS = ["product what it does", "technology how it works architecture",
                "founders team leadership", "customers case studies", "pricing plans"]


async def site_fetcher(company_name: str, *, web, domain: str = "", max_per_facet: int = 3) -> list[dict]:
    """Read the company's OWN site for DEPTH (product / technology / team / customers / pricing) —
    the dimensions news + Form-D miss. Domain-scoped Exa searches carrying highlights+body; stamped
    `corp_eng` (SELF-REPORTED tier — never outranks press/filings). Not a filing, so it attaches to
    the targeted company. Fail-safe → []."""
    prefix = (f"site:{domain} " if domain else f"{company_name} ")
    docs: list[dict] = []
    seen: set = set()
    for facet in _SITE_FACETS:
        try:
            results = await web.search(prefix + facet, max_results=max_per_facet)
        except Exception:   # noqa: BLE001
            continue
        for r in (results or []):
            text = _grounding_text(r)
            url = getattr(r, "url", "") or ""
            if not text or url in seen:
                continue
            seen.add(url)
            docs.append({
                "source_key": "web", "document_id": "site:" + _sha(url or text),
                "text": text,
                "facets": {"source_kind": "corp_eng", "web_role": "official", "url": url},
            })
    return docs


async def formd_fetcher(company_name: str, *, edgar, max_hits: int = 3) -> list[dict]:
    """EDGAR full-text Form D for `company_name` → candidate FILING docs (source_key='edgar' →
    `primary_filing`, controlling). Carries `issuer_name` so the pipeline's ER can accept a real
    match or REJECT an SPV/namesake. Fail-safe → []."""
    try:
        refs = await edgar._fts_formd(company_name, max_hits)
    except Exception:   # noqa: BLE001
        return []
    docs: list[dict] = []
    for ref in (refs or []):
        try:
            for dref in await edgar.list_documents(ref):
                raw = await edgar.fetch_artifact(dref)
                text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
                if not text.strip():
                    continue
                docs.append({
                    "source_key": "edgar", "document_id": "edgar:" + (ref.native_id or _sha(text)),
                    "text": text[:4000], "facets": {"source_kind": "filing"},
                    "issuer_name": ref.title or company_name, "strong_ids": {},
                })
        except Exception:   # noqa: BLE001
            continue
    return docs


async def enrich_one(company_id: str, company_name: str, dsn: str, *,
                     sources: tuple[str, ...] = ("news", "formd"),
                     dry_run: bool = True, tenant_id: str = "demo",
                     object_policy: str = "create") -> dict:
    """Run the live multi-source enrichment on ONE canonical company. Builds the tech-configured
    store + Exa web + EDGAR + tech authority policy, then calls the S2b pipeline with the S2a
    resolver. dry_run (default) plans + costs without spending. Returns the pipeline summary."""
    from api.claimgraph_tech import make_tech_claim_store
    from api.canonicalize import resolve_entity, resolve_conflicts
    from api.multisource_enrich import enrich_company
    from roster_kernel.runtime.build import build_web
    from roster_vertical.connectors.edgar import EdgarConnector
    from roster_vertical.authority import TechAuthorityPolicy
    from roster_vertical.claim_extract import extract_typed_claims

    store = make_tech_claim_store(dsn)
    web = build_web(mode="live", domains=(), recent=True)
    edgar = EdgarConnector()
    fetchers = []
    if "news" in sources:
        fetchers.append(lambda n: news_fetcher(n, web=web))
    if "formd" in sources:
        fetchers.append(lambda n: formd_fetcher(n, edgar=edgar))
    try:
        return await enrich_company(
            store=store, dsn=dsn, company_id=company_id, company_name=company_name,
            fetchers=fetchers, extract_fn=extract_typed_claims,
            resolve_entity_fn=resolve_entity, resolve_conflicts_fn=resolve_conflicts,
            authority_policy=TechAuthorityPolicy(), llm=None,
            tenant_id=tenant_id, dry_run=dry_run, object_policy=object_policy)
    finally:
        try:
            await store.close()
        except Exception:   # noqa: BLE001
            pass
