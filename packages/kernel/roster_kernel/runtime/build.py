"""Build providers by mode, and discover the active vertical.

Providers are always cassette-wrapped: `replay` (default) runs offline for free;
`record`/`live` use the real Anthropic/OpenAI/Tavily backends. So the same code
path serves dev/CI/eval and production — credits are opt-in via ROSTER_PROVIDER_MODE.
"""
from __future__ import annotations

import os
from pathlib import Path

from roster_kernel.providers.anthropic_llm import AnthropicLLM
from roster_kernel.providers.base import ProviderMode, resolve_mode
from roster_kernel.providers.embeddings import (
    OPENAI_EMBED_DIM,
    CassetteEmbedder,
    Embedder,
    FakeEmbedder,
    LocalEmbedder,
    OpenAIEmbedder,
)
from roster_kernel.providers.llm import CassetteLLM, LLMClient
from roster_kernel.providers.web_tavily import TavilyWebSearch
from roster_kernel.providers.websearch import CassetteWebSearch, CompositeWebSearch, WebSearchClient


def default_cassette_root() -> Path:
    return Path(os.environ.get("ROSTER_CASSETTE_ROOT", "evals/cassettes"))


def _route_by_name(model: str | None) -> str | None:
    """A model NAME picks its provider — so an explicit `claude-*` (e.g. the cheap layout model) stays
    Anthropic even when the global provider is switched to deepseek, and vice versa."""
    m = (model or "").lower()
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("claude"):
        return "anthropic"
    return None


def _build_inner_llm(model: str | None) -> LLMClient:
    """Provider seam. `ROSTER_LLM_PROVIDER` (default 'anthropic') selects the family; `ROSTER_LLM_MODEL`
    the model. An explicit model name overrides the provider (see `_route_by_name`). DeepSeek and OpenAI
    reuse the OpenAI-protocol client (DeepSeek is OpenAI-compatible via base_url). Default (no provider
    env, no model) → AnthropicLLM() exactly as before → byte-identical."""
    provider = _route_by_name(model) or os.environ.get("ROSTER_LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "deepseek":
        from roster_kernel.providers.openai_client import OpenAILLMClient
        return OpenAILLMClient(
            model=model or os.environ.get("ROSTER_LLM_MODEL", "deepseek-chat"),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("ROSTER_DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    if provider == "openai":
        from roster_kernel.providers.openai_client import OpenAILLMClient
        return OpenAILLMClient(model=model or os.environ.get("ROSTER_LLM_MODEL", "gpt-4o"))
    return AnthropicLLM(model=model) if model else AnthropicLLM()


def build_llm(*, mode: ProviderMode | str | None = None, cassette_root: Path | None = None,
              model: str | None = None) -> LLMClient:
    m = resolve_mode(mode)
    inner = None if m is ProviderMode.REPLAY else _build_inner_llm(model)
    return CassetteLLM(inner, cassette_root=cassette_root or default_cassette_root(),
                       namespace="llm", mode=m)


def build_embedder(*, mode: ProviderMode | str | None = None, cassette_root: Path | None = None,
                   dim: int = OPENAI_EMBED_DIM) -> Embedder:
    m = resolve_mode(mode)
    inner = None if m is ProviderMode.REPLAY else _live_embedder(dim)
    return CassetteEmbedder(inner, cassette_root=cassette_root or default_cassette_root(),
                            namespace="embed", dim=dim, mode=m)


def _live_embedder(dim: int) -> Embedder:
    """The live embedding backend. OpenAI when `OPENAI_API_KEY` is set (production default). Otherwise
    (a DeepSeek-only deployment — DeepSeek has NO embeddings API) fall back to a KEYLESS embedder so
    the retrieval plumbing never crashes on a missing key: the local sentence-transformers model when
    installed (real semantics), else the deterministic hash embedder (no dep, correct dim, but a
    non-semantic rerank — flagged as degraded). `ROSTER_EMBEDDER` forces a choice: 'openai'|'local'|
    'hash'."""
    choice = os.environ.get("ROSTER_EMBEDDER", "").strip().lower()
    if choice == "openai" or (not choice and os.environ.get("OPENAI_API_KEY")):
        return OpenAIEmbedder(dim=dim)
    if choice == "hash":
        return FakeEmbedder(dim=dim)
    if choice == "local":
        return LocalEmbedder()
    # Auto (no ROSTER_EMBEDDER, no OPENAI_API_KEY): prefer the local model, fall back to hash.
    try:
        import sentence_transformers  # noqa: F401  (probe the optional dep without downloading)
        return LocalEmbedder()
    except Exception:  # noqa: BLE001
        return FakeEmbedder(dim=dim)


def build_web(*, mode: ProviderMode | str | None = None,
              cassette_root: Path | None = None,
              domains: tuple[str, ...] | list[str] | None = None,
              recent: bool = False) -> WebSearchClient:
    m = resolve_mode(mode)
    # Prefer Exa (neural search) when its key is set; else Tavily. Same WebSearchClient port.
    # `domains` (from the vertical) restricts Exa to a trusted-sources whitelist. `recent` (freshness
    # flag) biases the web leg to the last ~2 years so "latest state of the world" questions get
    # current news/pages, not the most-linked 2024-era write-ups (the corpus still holds the history).
    inner = None
    if m is not ProviderMode.REPLAY:
        def _exa():
            from roster_kernel.providers.exa_web import ExaWebSearch
            _floor = ""
            if recent:
                import datetime
                # ~4-month floor: tight enough that Exa's candidate set is THIS quarter's releases,
                # so newest-first sorting surfaces the very latest models (not last spring's overview);
                # the corpus still holds the full history for non-"latest" questions.
                _floor = (datetime.date.today() - datetime.timedelta(days=120)).isoformat() + "T00:00:00.000Z"
            return ExaWebSearch(include_domains=list(domains or []), start_published_date=_floor)

        def _ddg():
            from roster_kernel.providers.ddg_web import DuckDuckGoWebSearch
            return DuckDuckGoWebSearch()

        _has_exa, _has_tav = bool(os.environ.get("EXA_API_KEY")), bool(os.environ.get("TAVILY_API_KEY"))
        provider = os.environ.get("ROSTER_WEB_PROVIDER", "").strip().lower()
        if provider == "ddg":                                   # forced single provider (override)
            inner = _ddg()
        elif provider == "exa" and _has_exa:
            inner = _exa()
        elif provider == "tavily" and _has_tav:
            inner = TavilyWebSearch(time_range="year" if recent else "")
        else:
            # DEFAULT = ADDITIVE: the best keyed provider (credible, whitelisted) PLUS the keyless
            # DuckDuckGo (open-web breadth), fanned out concurrently + merged. Downstream tier-grading
            # + span-verification decide what's actually cited, so credible sources still lead while
            # DDG adds reach. With NO paid key, DDG alone runs the web leg (free).
            # ADDITIVE composite: run EVERY keyed engine concurrently for maximal, diverse coverage
            # (was `if Exa elif Tavily`, which left a working paid provider idle). The composite dedupes
            # by URL and the tier/span/rerank gates still decide what's actually cited.
            _has_brave = bool(os.environ.get("BRAVE_API_KEY"))
            # Tavily is DISABLED by default (its key is currently out of credits → 402 on every call,
            # wasting a leg + latency). Re-enable by setting ROSTER_ENABLE_TAVILY=1 once credits are
            # restored — no code change needed then.
            _tav_on = _has_tav and os.environ.get("ROSTER_ENABLE_TAVILY", "").lower() in ("1", "true", "yes")
            clients = []
            if _has_exa:
                clients.append(_exa())               # depth: full text + query-aware highlights
            if _tav_on:
                clients.append(TavilyWebSearch(time_range="year" if recent else ""))
            if _has_brave:
                # Brave Search API — the free/cheap open-web BREADTH leg REPLACING DuckDuckGo, whose
                # HTML scrape is blocked from datacenter IPs (returns 0 in prod). Real REST API, unblocked.
                from roster_kernel.providers.brave_web import BraveWebSearch
                clients.append(BraveWebSearch())
            if not clients:
                # NO keyed provider at all → DuckDuckGo is the last-resort free leg (may be blocked).
                clients.append(_ddg())
            if len(clients) == 1:
                inner = clients[0]
            else:
                # BRAVE-DISCOVERS → EXA-HYDRATES: Brave (and any snippet-only engine) returns URLs with
                # ~300-char descriptions that can't ground a quote. When Exa is present, hydrate those thin
                # discoveries to full text via Exa /contents — so a Brave-found page becomes citable while
                # Exa keeps crediting Brave for the discovery. The composite hydrates ONLY thin, already-
                # deduped results (no new URL → no duplication). Fail-safe: hydration error keeps snippets.
                # HYDRATION is behind ROSTER_WEB_HYDRATE (default OFF): the current per-leg implementation
                # adds an Exa /contents round-trip to EVERY web leg (8-12), which pushed research past
                # Railway's ~300s edge timeout (502s). Keep the capability but default-off until it's
                # redesigned to hydrate ONCE post-retrieval (after all legs merge), not per-leg.
                _hydrator = None
                if _has_exa and os.environ.get("ROSTER_WEB_HYDRATE", "").lower() in ("1", "true", "yes"):
                    from roster_kernel.providers.exa_web import ExaWebSearch as _ExaH
                    _h = _ExaH()
                    _hydrator = (lambda urls, q, _h=_h: _h.get_contents(urls, query=q))
                inner = CompositeWebSearch(clients, hydrator=_hydrator)
    return CassetteWebSearch(inner, cassette_root=cassette_root or default_cassette_root(),
                             namespace="web", mode=m)


def load_active_vertical(name: str | None = None):
    """Discover installed verticals via the `roster.verticals` entry point and
    return the one named by ROSTER_ACTIVE_VERTICAL (single-vertical-per-deployment)."""
    from importlib.metadata import entry_points

    want = name or os.environ.get("ROSTER_ACTIVE_VERTICAL")
    eps = list(entry_points(group="roster.verticals"))
    if not eps:
        raise RuntimeError("no roster.verticals installed")
    ep = next((e for e in eps if e.name == want), None) if want else eps[0]
    if ep is None:
        raise RuntimeError(f"active vertical {want!r} not found (have: {[e.name for e in eps]})")
    return ep.load()
