"""Vertical-owned company-reader vocabulary for the Phase-1 deep web leg."""
from __future__ import annotations


DOMAIN_RESOLUTION_PROMPT = (
    "Pick the canonical official website domain for the named company from the candidate pages. "
    "Return only a domain that is clearly the company's own website. Prefer the company's homepage "
    "over social profiles, app stores, help-center mirrors, articles, directories, and search "
    "pages. If the candidates do not clearly include the official site, return an empty domain."
)


ATTRIBUTION_ADDENDUM = (
    "COMPANY-READER ATTRIBUTION: Treat company-site, docs, blog, changelog, pricing, and "
    "customer-page claims as the company says / self-reported unless the same claim is "
    "independently corroborated. For ARR, revenue, valuation, fundraising, customer-count, "
    "market-leadership, or competitive-position claims, use independent press/database/filing "
    "support when available; otherwise explicitly label the claim as self-reported by the "
    "company. Do not present company marketing copy as independent validation."
)


COMPANY_READER = {
    "domain_query_template": "{company} official website",
    "domain_prompt": DOMAIN_RESOLUTION_PROMPT,
    "internal": {
        "founders_team": "{company} founders team leadership about",
        "product": "{company} product platform features use cases",
        "technology": "{company} technology architecture docs API model stack",
        "pricing": "{company} pricing plans enterprise",
        "customers": "{company} customers case studies testimonials",
        "blog_changelog": "{company} blog changelog release notes engineering",
    },
    "external": {
        "funding_investors_valuation": (
            "{company} funding investors valuation site:crunchbase.com OR site:techcrunch.com "
            "OR site:reuters.com OR site:bloomberg.com OR site:forbes.com"
        ),
        "competitors_traction": (
            "{company} competitors traction customers market share site:cbinsights.com "
            "OR site:theinformation.com OR site:techcrunch.com OR site:forbes.com"
        ),
    },
    "attribution_addendum": ATTRIBUTION_ADDENDUM,
    "max_queries": 8,
    "max_pages": 18,
    "max_results_per_query": 3,
    "max_chars": 12000,
    "max_chunks_per_page": 5,
    "concurrency": 3,
    "deadline_s": 25.0,
}
