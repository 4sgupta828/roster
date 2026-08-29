"""Expert-newsletter/blog connector — curated EXPERT OPINION / tech-trend foresight (keyless RSS/Atom).

This is a FEED connector, NOT a search API: there is no keyword endpoint. It ingests recent items
from a CURATED ALLOWLIST of public, full-text-friendly expert RSS/Atom feeds on deep tech. Keyword
relevance is handled later by retrieval — the connector's job is to pull RECENT expert items with
clean provenance (author + publication + date).

discover_entities → fetch the allowlisted feeds (all, or those loosely matching window["query"]) and
emit one EntityRef per recent item (or use injected fixture items offline). list_documents → one
labeled expert-analysis document per item. fetch_artifact → the assembled markdown bytes. Everything
grades to the "expert_analysis" tier via source_kind="essay" (tier wired separately by the orchestrator).

XML is parsed by the shared stdlib RSS/Atom parser in `._feed` (matching by tag LOCAL name so RSS
<item> and Atom <entry> both parse), the same parser the eng_blog connector uses.
"""
from __future__ import annotations

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import expert_feed_doc
from ._feed import parse_feed
from ._http import HttpStrategy

# ── Curated starter allowlist ────────────────────────────────────────────────────────────────────
# CURATABLE. ONLY public, full-text-friendly expert RSS/Atom feeds belong here — named experts'
# analysis/foresight on deep tech. Do NOT add paywalled or feed-sharing-forbidden sources
# (e.g. Stratechery, SemiAnalysis) — public feeds only.
FEEDS: list[str] = [
    "https://simonwillison.net/atom/everything/",          # Simon Willison — LLMs, tooling
    "https://magazine.sebastianraschka.com/feed",          # Sebastian Raschka — Ahead of AI
    "https://lilianweng.github.io/index.xml",              # Lilian Weng — deep-dive ML essays
    "https://jack-clark.net/feed/",                         # Jack Clark — Import AI
    "https://huggingface.co/blog/feed.xml",                # Hugging Face blog
    "https://thegradient.pub/rss/",                        # The Gradient
    "https://huyenchip.com/feed.xml",                       # Chip Huyen — ML systems
    "https://bair.berkeley.edu/blog/feed.xml",             # Berkeley BAIR blog
    "https://distill.pub/rss.xml",                          # Distill (archival, high quality)
    "https://www.deepmind.com/blog/rss.xml",               # Google DeepMind blog
    "https://blog.research.google/feeds/posts/default",     # Google Research blog
    "https://openai.com/blog/rss.xml",                     # OpenAI blog
    "https://www.anthropic.com/rss.xml",                   # Anthropic
    "https://karpathy.github.io/feed.xml",                 # Andrej Karpathy
    "https://eugeneyan.com/rss/",                           # Eugene Yan — applied ML
    "https://www.interconnects.ai/feed",                   # Nathan Lambert — Interconnects
    "https://newsletter.pragmaticengineer.com/feed",        # Gergely Orosz — eng leadership
    "https://www.oneusefulthing.org/feed",                 # Ethan Mollick — AI in practice
    "https://jvns.ca/atom.xml",                            # Julia Evans — systems
    "https://ai.googleblog.com/atom.xml",                  # Google AI (legacy atom)
    "https://blog.ml.cmu.edu/feed/",                       # CMU ML blog
    "https://www.fast.ai/index.xml",                       # fast.ai
]


class ExpertFeedConnector:
    key = "expert_feed"

    def __init__(self, *, items: list[dict] | None = None, page_size: int = 30):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for r in (items or []):
            if expert_feed_doc.item_id(r):
                self._by_id[expert_feed_doc.item_id(r)] = r

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
                try:                                          # best-effort: one bad feed ≠ dead batch
                    recs = parse_feed(await self.fetch_strategy.fetch(url))
                except Exception:
                    continue
                for r in recs[:per]:
                    if expert_feed_doc.item_id(r):
                        items.append(r)
                if len(items) >= 50:
                    break
            items = items[:50]                                # cap total recent items per pass
            for r in items:
                self._by_id[expert_feed_doc.item_id(r)] = r
        return [EntityRef(source_key=self.key, native_id=expert_feed_doc.item_id(r),
                          title=expert_feed_doc.title(r), facets=expert_feed_doc.facets(r))
                for r in items if expert_feed_doc.item_id(r)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=expert_feed_doc.facets(r) if r else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return expert_feed_doc.to_markdown(r).encode("utf-8")
