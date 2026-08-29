"""Tech UI declaration — renders per-vertical with zero app edits."""
from __future__ import annotations

from . import links as _links


class _EntityView:
    def __init__(self, entity_type, columns, sections):
        self.entity_type = entity_type
        self._c, self._s = columns, sections
    def list_columns(self): return list(self._c)
    def detail_sections(self): return list(self._s)


class TechUI:
    def source_url(self, document_id, quote=None, facets=None):
        return _links.source_url(document_id, quote, facets)

    def coverage_plan(self):
        """Declared coverage roadmap for the tech vertical — what sectors/sources are live
        today versus planned. Surfaced read-only by the /admin/coverage handler."""
        return {
            "sectors": {
                "covered": ["ai"],
                "planned": ["fintech", "biotech", "semiconductors", "climate"],
            },
            "sources": {
                "live": ["arxiv", "openalex", "edgar"],
                "planned": ["edgar Form D", "patentsview", "github", "semantic_scholar",
                            "crossref", "gdelt", "hackernews"],
            },
        }

    def console(self):
        return {
            "heading": "What does the evidence say?",
            "placeholder": "Ask a tech / startup / market diligence question…",
            "disclaimer": {
                "gate": ("Roster is a research tool for investors and analysts. It finds and summarizes "
                         "published evidence — filings, patents, papers, code, and news — for "
                         "informational purposes only. It is NOT investment advice, not a recommendation "
                         "to buy or sell, and not a substitute for independent professional judgment or "
                         "your own diligence. Answers are AI-generated and may be incomplete, out of date, "
                         "or wrong; always verify against the cited primary sources. By continuing you "
                         "accept sole responsibility for how you use the information."),
                "footer": ("For informational purposes only — not investment advice. Verify every answer "
                           "against the cited primary sources before any decision."),
                "answer": ("AI-generated summary of published evidence — informational only, not investment "
                           "advice. Market sentiment is a signal, not fact. Verify against the cited sources."),
            },
            "examples": [
                "Assess the competitive position and funding trajectory of an agentic-AI coding startup.",
                "What is the state of the art in on-device LLM inference, and who leads — labs, startups, or incumbents?",
                "What revenue and losses did the latest AI-infrastructure S-1 disclose?",
                "Which companies hold the key patents in retrieval-augmented generation?",
                "How much open-source traction does a given model framework have versus its competitors?",
                "What does recent coverage suggest about market sentiment toward AI-agent startups?",
                "Compare two foundation-model companies on benchmarks, funding, and team.",
                "What are the disclosed risk factors for a public semiconductor-design company?",
            ],
        }

    def navigation(self):
        return [
            {"key": "companies", "label": "Companies", "entity_type": "company"},
            {"key": "research", "label": "Research", "view": "agent"},
        ]

    def search_facets(self):
        return [
            {"key": "sector", "label": "Sector", "control": "select"},
            {"key": "entity_type", "label": "Type", "control": "select"},
            {"key": "stage", "label": "Stage", "control": "select"},
            {"key": "geography", "label": "Geography", "control": "select"},
        ]

    def entity_views(self):
        return [
            _EntityView(
                "company",
                columns=[
                    {"key": "native_id", "label": "Company", "kind": "text"},
                    {"key": "sector", "label": "Sector", "kind": "text"},
                    {"key": "stage", "label": "Stage", "kind": "badge"},
                    {"key": "geography", "label": "Geo", "kind": "text"},
                ],
                sections=[
                    {"title": "Overview", "fields": ["native_id", "sector", "stage", "geography"]},
                    {"title": "Summary", "fields": ["summary"]},
                ],
            ),
            _EntityView(
                "paper",
                columns=[
                    {"key": "native_id", "label": "arXiv", "kind": "text"},
                    {"key": "category", "label": "Category", "kind": "text"},
                    {"key": "year", "label": "Year", "kind": "text"},
                ],
                sections=[
                    {"title": "Overview", "fields": ["native_id", "category", "year"]},
                    {"title": "Abstract", "fields": ["summary"]},
                ],
            ),
        ]

    def citation_renderers(self):
        return {"block_span": "source-quote", "url": "web-link"}

    def deliverable_renderers(self):
        return {"memo": "memo-doc", "comparison_table": "table-grid"}
