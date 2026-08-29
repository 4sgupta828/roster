"""LLM provider port + cassette wrapper + a deterministic fake.

Ported (clean, domain-free) from the prior system's llm/interface.py +
eval/cassette.py. The port is pure (no DB, no domain concepts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel

from .base import ProviderMode, guard_live, resolve_mode
from .cassette import Cassette, hash_request


@dataclass
class LLMResult:
    parsed: BaseModel
    raw: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    model: str = "fake"


@runtime_checkable
class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> LLMResult: ...


class FakeLLM:
    """Deterministic, offline LLM for unit tests.

    Given a builder that maps (system, messages) → a `response_format` instance,
    it returns that with zero cost. No network, no credits, no cassette needed.
    """

    def __init__(self, builder: Callable[[str, list[dict], type[BaseModel]], BaseModel]):
        self._builder = builder

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> LLMResult:
        return LLMResult(parsed=self._builder(system, messages, response_format), model="fake")


class CassetteLLM:
    """Wrap an inner LLMClient with replay/record/live behavior."""

    def __init__(
        self,
        inner: LLMClient | None,
        *,
        cassette_root: Path,
        namespace: str,
        mode: ProviderMode | str | None = None,
    ):
        self._inner = inner
        self._mode = resolve_mode(mode)
        self._cassette = Cassette(root=cassette_root, namespace=namespace)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> LLMResult:
        key = hash_request("llm", system, messages, response_format.__name__, temperature)
        if self._mode is ProviderMode.REPLAY:
            stored = self._cassette.replay(key, hint=f"response_format={response_format.__name__}")
            return LLMResult(
                parsed=response_format.model_validate(stored["parsed"]),
                cost_usd=Decimal(str(stored.get("cost_usd", "0"))),
                input_tokens=stored.get("input_tokens", 0),
                output_tokens=stored.get("output_tokens", 0),
                model=stored.get("model", "replay"),
            )
        guard_live(self._mode)
        if self._inner is None:
            raise RuntimeError("CassetteLLM in record/live mode requires an inner client")
        result = await self._inner.complete(
            system=system,
            messages=messages,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if self._mode is ProviderMode.RECORD:
            self._cassette.record(key, {
                "parsed": result.parsed.model_dump(mode="json"),
                "cost_usd": str(result.cost_usd),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "model": result.model,
            })
        return result
