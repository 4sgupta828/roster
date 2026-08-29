"""OpenAILLMClient — a real, DIFFERENT-FAMILY LLMClient (OpenAI structured output).

roster's cross-family GROUNDING GATE (research/grounding_gate.py) and the derive VALIDITY JUDGE
(research/reason.py) are strongest when graded by a model from a DIFFERENT family than the drafter
(Anthropic) — a second, uncorrelated mind. This adapter implements the `LLMClient` protocol
(providers/llm.py) on top of OpenAI so it can be wired as `derive_judge_llm`.

Construction mirrors `openai_llm.py::OpenAIJsonLLM` (lazy SDK import, OPENAI_API_KEY handling,
AsyncOpenAI). Structured output is obtained via OpenAI's json_schema response_format built from the
pydantic `response_format.model_json_schema()` (non-strict — roster's judge shapes default their
fields, so a partial/defaulted object must still parse). If a model rejects json_schema mode, we
retry ONCE in plain json_object mode with the schema described in the system prompt (robust across
older models). Fail cleanly (raise on API/parse error) — every caller already fail-safes on a judge
error / None judge.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from pydantic import BaseModel

from . import _jsonsafe
from .llm import LLMResult

DEFAULT_MODEL = os.environ.get("ROSTER_JUDGE_MODEL", "gpt-4o")


class OpenAILLMClient:
    def __init__(self, *, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None):
        self._model = model or os.environ.get("ROSTER_JUDGE_MODEL", "gpt-4o")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        # base_url lets this same OpenAI-protocol client target ANY OpenAI-compatible endpoint —
        # notably DeepSeek (https://api.deepseek.com). None → OpenAI's default endpoint (byte-identical).
        self._base_url = base_url or os.environ.get("ROSTER_OPENAI_BASE_URL") or None
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            from openai import AsyncOpenAI   # lazy, optional dep (already used for embeddings)
            kw: dict = {}
            if self._api_key:
                kw["api_key"] = self._api_key
            if self._base_url:
                kw["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kw)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> LLMResult:
        self._ensure()
        assert self._client is not None
        # OpenAI keeps `system` as a role in the messages list; fold any system-role turns into it
        # (mirror anthropic_llm) and keep only user/assistant turns as the conversation.
        sys_parts = [system] + [m["content"] for m in messages if m.get("role") == "system"]
        system_text = "\n\n".join(p for p in sys_parts if p)
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in messages if m.get("role") in ("user", "assistant")]

        schema = response_format.model_json_schema()
        base_messages = ([{"role": "system", "content": system_text}] if system_text else []) + convo
        common: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            **({"temperature": temperature} if temperature is not None else {}),
        }

        _t0 = time.perf_counter()
        resp = None
        content: str | None = None
        try:
            # Primary path: OpenAI structured output via json_schema (non-strict so defaulted/partial
            # objects parse — the judge shapes default their container fields).
            resp = await self._client.chat.completions.create(
                messages=base_messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_format.__name__,
                        "schema": schema,
                        "strict": False,
                    },
                },
                **common,
            )
            content = resp.choices[0].message.content
            obj = _jsonsafe.loads(content)   # tolerant of LLM invalid-\escape (markdown/LaTeX/paths)
            parsed = response_format.model_validate(obj)
        except Exception:
            # Fallback: some models reject json_schema mode (or the schema). Retry ONCE in plain
            # json_object mode with the schema DESCRIBED in the system prompt.
            fallback_system = (
                system_text
                + "\n\nReturn ONLY a single JSON object that conforms to this JSON Schema:\n"
                + json.dumps(schema)
            )
            fb_messages = [{"role": "system", "content": fallback_system}] + convo
            resp = await self._client.chat.completions.create(
                messages=fb_messages,
                response_format={"type": "json_object"},
                **common,
            )
            content = resp.choices[0].message.content
            obj = _jsonsafe.loads(content)   # tolerant of LLM invalid-\escape (markdown/LaTeX/paths)
            parsed = response_format.model_validate(obj)  # raise on parse error — caller fail-safes

        _ms = int((time.perf_counter() - _t0) * 1000)
        usage = getattr(resp, "usage", None)
        return LLMResult(
            parsed=parsed,
            input_tokens=(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
            output_tokens=(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
            cost_usd=Decimal(0),   # priced by the caller's cost governor if needed
            latency_ms=_ms,
            model=self._model,
        )
