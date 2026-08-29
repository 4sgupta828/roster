"""Tech connectors — source-specific selectors/API mapping + normalization ONLY.

Each implements the kernel `Connector` protocol (discover_entities → list_documents →
fetch_artifact). Domain nouns live only in returned refs' facets/extra. All are
fixture-injectable so they run offline in tests; live use fetches over HttpStrategy.
"""
from .arxiv import ArxivConnector
from .companies_house import CompaniesHouseConnector
from .crossref import CrossrefConnector
from .edgar import EdgarConnector
from .eng_blog import EngBlogConnector
from .gdelt import GdeltConnector
from .github import GithubConnector
from .hackernews import HackerNewsConnector
from .huggingface import HuggingFaceConnector
from .lobsters import LobstersConnector
from .nih_reporter import NihReporterConnector
from .nsf import NsfConnector
from .openalex import OpenAlexConnector
from .openreview import OpenReviewConnector
from .patentsview import PatentsViewConnector
from .podcast import PodcastConnector
from .reddit import RedditConnector
from .semantic_scholar import SemanticScholarConnector
from .stackexchange import StackExchangeConnector
from .expert_feed import ExpertFeedConnector
from .uspto import UsptoConnector
from .wikidata import WikidataConnector
from .wikipedia import WikipediaConnector
from .yc import YcConnector

__all__ = ["ArxivConnector", "CompaniesHouseConnector", "CrossrefConnector", "EdgarConnector",
           "EngBlogConnector",
           "ExpertFeedConnector", "GdeltConnector", "GithubConnector", "HackerNewsConnector",
           "HuggingFaceConnector", "LobstersConnector", "NihReporterConnector", "NsfConnector",
           "OpenAlexConnector",
           "OpenReviewConnector", "PatentsViewConnector", "PodcastConnector", "RedditConnector",
           "SemanticScholarConnector", "StackExchangeConnector", "UsptoConnector", "WikidataConnector",
           "WikipediaConnector", "YcConnector"]
