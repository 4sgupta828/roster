"""Company engineering-blog connector — curated SOTA eng blogs (keyless RSS/Atom, self-reported tier).

Sibling of `expert_feed`, but a DIFFERENT population and a LOWER tier. `expert_feed` carries NAMED
individuals' essays (`expert_analysis`, rank 3). This carries COMPANY engineering blogs — Netflix,
Cloudflare, Meta Engineering, … — which are self-reported corporate accounts of how an org builds,
so they grade at `technical_signal` (rank 2, via `source_kind="corp_eng"`): useful and retrievable,
never able to outrank fact-checked press/a filing on a claim about the org's own tech, never controlling.

This is a FEED connector, not a search API. It ingests recent items from a CURATED, EMPIRICALLY-VETTED
allowlist (every feed below was verified live + full-text + connector-compatible + pure-engineering —
precision over recall, no marketing/summary-only/SPA-dead feeds). Shares the stdlib RSS/Atom parser in
`._feed` with expert_feed. A dead/empty feed is LOGGED (not silently skipped) so a Tier-A source that
rots is observable rather than starving the lane invisibly.
"""
from __future__ import annotations

import logging

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import eng_blog_doc
from ._feed import parse_feed
from ._http import HttpStrategy

_log = logging.getLogger(__name__)

# ── Curated SOTA allowlist (empirically verified: live + FULL-TEXT + connector-compatible + pure-eng) ──
# INCLUSION BAR: official public RSS/Atom only; a majority of recent posts are engineering deep-dives
# (architecture / distributed systems / infra / ML platforms / reliability / security / performance),
# NOT product/launch/hiring/SEO; active; full article body in the feed (not a teaser); not paywalled.
# Do NOT add: summary-only feeds (Stripe/Spotify/Databricks/Tailscale/Jane Street — teaser-only), SPA
# sites with no feed (TigerBeetle/ClickHouse/Cockroach/Shopify), Medium /pub/feed (serves HTML to
# servers — only custom-domain Medium works, e.g. netflixtechblog.com), or vendor firehoses
# (AWS News/Google Cloud/Azure/Docker/NVIDIA — product/announcement-heavy). Re-audit annually.
FEEDS: list[str] = [
    "https://netflixtechblog.com/feed",        # Netflix — streaming/data-platform/resiliency (Medium custom domain)
    "https://blog.cloudflare.com/rss/",        # Cloudflare — networking/edge/security/performance
    "https://engineering.fb.com/feed/",        # Meta Engineering — hyperscale infra/data/AI systems
    "https://dropbox.tech/feed",               # Dropbox Tech — storage/sync/large migrations
    "https://github.blog/engineering/feed/",   # GitHub Engineering — dev-infra/platform/perf/security
    "https://slack.engineering/feed/",         # Slack Engineering — platform/data-pipeline/build systems
    "https://www.etsy.com/codeascraft/rss",    # Etsy Code as Craft — classic production-systems blog
    "https://engineering.grab.com/feed.xml",   # Grab Engineering — logistics-scale backend/data/ML
    "https://fly.io/blog/feed.xml",            # Fly.io — infra-native systems/networking deep dives
    "https://planetscale.com/blog/rss.xml",    # PlanetScale — database internals (Vitess/MySQL at scale)
    "https://oxide.computer/blog/feed",        # Oxide Computer — hardware/OS/hypervisor long-form
    "https://ably.com/blog/rss.xml",           # Ably — distributed-systems / realtime engineering
]


class EngBlogConnector:
    key = "eng_blog"

    def __init__(self, *, items: list[dict] | None = None, page_size: int = 30):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for r in (items or []):
            if eng_blog_doc.item_id(r):
                self._by_id[eng_blog_doc.item_id(r)] = r

    @staticmethod
    def _select_feeds(query: str) -> list[str]:
        """Loosely narrow the allowlist by query tokens (matched against the feed URL/domain).

        Relevance is retrieval's job; this only trims the fetch fan-out when a query is given. No
        match → fall back to the full allowlist so a query never silently starves the ingest lane.
        """
        toks = [t for t in query.lower().split() if len(t) > 2]
        if not toks:
            return list(FEEDS)
        hit = [u for u in FEEDS if any(t in u.lower() for t in toks)]
        return hit or list(FEEDS)

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        window = window or {}
        query = (window.get("query") or "").strip()
        if self._by_id and not query:
            items = list(self._by_id.values())
        else:
            limit = int(window.get("limit", self._page_size) or self._page_size)
            feeds = self._select_feeds(query)
            per = max(1, limit // max(1, len(feeds)))
            items = []
            for url in feeds:
                try:
                    recs = parse_feed(await self.fetch_strategy.fetch(url))
                except Exception as e:   # noqa: BLE001 — one bad feed ≠ dead batch, but LOG it (don't rot silently)
                    _log.warning("eng_blog: feed failed (%s) — %s", url, str(e)[:120])
                    continue
                if not recs:
                    _log.warning("eng_blog: feed returned 0 items — %s", url)
                for r in recs[:per]:
                    if eng_blog_doc.item_id(r):
                        items.append(r)
                if len(items) >= 50:
                    break
            items = items[:50]                                # cap total recent items per pass
            for r in items:
                self._by_id[eng_blog_doc.item_id(r)] = r
        return [EntityRef(source_key=self.key, native_id=eng_blog_doc.item_id(r),
                          title=eng_blog_doc.title(r), facets=eng_blog_doc.facets(r))
                for r in items if eng_blog_doc.item_id(r)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=eng_blog_doc.facets(r) if r else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return eng_blog_doc.to_markdown(r).encode("utf-8")
