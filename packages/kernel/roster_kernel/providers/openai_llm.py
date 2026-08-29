"""OpenAIJsonLLM — a minimal async OpenAI client in JSON mode.

Used by the fallback grounder (research/fallback_grounder.py): when the primary (Anthropic) agent
abstains despite gathered evidence, a SECOND model atomizes the evidence into cited claims. factra's
bake-off found a same-model re-ask unreliable at this (Sonnet 0/8) where a second model succeeds —
hence a distinct provider. Lazy-imports the SDK (optional dep, already used for embeddings).
"""
from __future__ import annotations

import os

from . import _jsonsafe


class OpenAIJsonLLM:
    def __init__(self, *, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            from openai import AsyncOpenAI   # lazy, optional dep
            self._client = AsyncOpenAI(api_key=self._api_key) if self._api_key else AsyncOpenAI()

    async def complete_json(self, *, model: str, system: str, user: str) -> dict:
        """One JSON-mode chat completion → parsed dict. No temperature (newer models reject
        non-default values); the prompts are strongly constrained (JSON mode)."""
        self._ensure()
        assert self._client is not None
        resp = await self._client.chat.completions.create(
            model=model, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        return _jsonsafe.loads(resp.choices[0].message.content)   # tolerant of invalid-\escape
