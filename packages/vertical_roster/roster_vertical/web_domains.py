"""Trusted web-search domains for the tech vertical + venue-authority facet stamps.

When set, web search is RESTRICTED to these — the corpus is augmented only with high-quality
sources, never the open web. `WEB_DOMAIN_FACETS` stamps venue authority as structural metadata
so web evidence is graded by `evidence_kind.classify` like corpus evidence.
"""
from __future__ import annotations

TRUSTED_WEB_DOMAINS: tuple[str, ...] = (
    # Primary / regulatory
    "sec.gov", "uspto.gov", "patents.google.com",
    # Primary-source LAB announcements — the horse's-mouth for new-model releases (news tier)
    "openai.com", "anthropic.com", "blog.google", "deepmind.google", "ai.meta.com",
    "mistral.ai", "x.ai", "moonshot.ai", "deepseek.com", "qwen.ai", "z.ai",
    # Verified structured — scholarly / benchmarks / code
    "arxiv.org", "openalex.org", "semanticscholar.org", "crossref.org",
    "paperswithcode.com", "mlcommons.org", "huggingface.co", "github.com",
    # Live leaderboards — the current "who leads" signal for fast-moving model races
    "lmarena.ai", "artificialanalysis.ai",
    # Analysis — reputable trade / business press
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "theinformation.com",
    "techcrunch.com", "arstechnica.com", "nature.com", "ieee.org",
    "theverge.com", "venturebeat.com",
    # Structured company/funding profiles (public pages)
    "crunchbase.com", "dealroom.co", "pitchbook.com",
)

# domain → facets stamped on web-retrieved blocks (venue authority as structural metadata).
WEB_DOMAIN_FACETS: dict[str, dict] = {
    "sec.gov": {"source_kind": "filing"},
    "uspto.gov": {"source_kind": "patent"},
    "patents.google.com": {"source_kind": "patent"},
    "arxiv.org": {"source_kind": "preprint"},
    "openalex.org": {"source_kind": "paper", "is_peer_reviewed": "true"},
    "semanticscholar.org": {"source_kind": "paper"},
    "paperswithcode.com": {"source_kind": "benchmark"},
    "mlcommons.org": {"source_kind": "benchmark"},
    "lmarena.ai": {"source_kind": "benchmark"},
    "artificialanalysis.ai": {"source_kind": "benchmark"},
    # lab announcement blogs → news tier (a release announcement, not an independent benchmark)
    "openai.com": {"source_kind": "news"}, "anthropic.com": {"source_kind": "news"},
    "blog.google": {"source_kind": "news"}, "deepmind.google": {"source_kind": "news"},
    "ai.meta.com": {"source_kind": "news"}, "mistral.ai": {"source_kind": "news"},
    "x.ai": {"source_kind": "news"}, "moonshot.ai": {"source_kind": "news"},
    "deepseek.com": {"source_kind": "news"}, "z.ai": {"source_kind": "news"},
    "theverge.com": {"source_kind": "news"},
    "venturebeat.com": {"source_kind": "news"},
    "github.com": {"source_kind": "code"},
    "reuters.com": {"source_kind": "news"},
    "bloomberg.com": {"source_kind": "news"},
    "ft.com": {"source_kind": "news"},
    "wsj.com": {"source_kind": "news"},
    "techcrunch.com": {"source_kind": "news"},
    "theinformation.com": {"source_kind": "news"},
    # SOCIAL / signal tier — public posts on these are perception, NOT authoritative fact. Stamped
    # `source_kind: social` → evidence_kind.classify → "sentiment_signal" (lowest tier, never
    # controlling), while still a CATALOGUED venue so the open-web denoise boost surfaces them above
    # unknown SEO domains. Reached only via the open-web leg (not in TRUSTED_WEB_DOMAINS, so the
    # whitelisted OFF path is unchanged). LinkedIn public pages, X, Reddit, HN.
    "linkedin.com": {"source_kind": "social"},
    "x.com": {"source_kind": "social"},
    "twitter.com": {"source_kind": "social"},
    "reddit.com": {"source_kind": "social"},
    "news.ycombinator.com": {"source_kind": "social"},
}
