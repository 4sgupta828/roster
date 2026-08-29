"""Provider ports — the single seam for every METERED external effect.

All paid/external calls (LLM completions, embeddings, web search) go through a
typed port here. Each port has a cassette wrapper with three modes:

    replay  → read a recorded response from disk; no network, ZERO credits.
              The DEFAULT for all dev/CI/eval. A miss raises CassetteMiss.
    record  → make the real call AND persist the response to a cassette.
    live    → make the real call (production).

Mode comes from ROSTER_PROVIDER_MODE (default "replay"). Under pytest, a `live`
call raises LiveCallForbidden unless ROSTER_ALLOW_LIVE=1 — so tests can never
silently spend credits.

This package is domain-agnostic: it knows nothing about any vertical.
"""
from .base import (
    LiveCallForbidden,
    ProviderMode,
    resolve_mode,
)

__all__ = ["ProviderMode", "resolve_mode", "LiveCallForbidden"]
