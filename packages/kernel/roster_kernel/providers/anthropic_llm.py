"""AnthropicLLM — a real LLMClient (structured output via forced tool-use).

Anthropic has no JSON mode; the robust pattern is a single tool whose input_schema
IS the response_format, with tool_choice forcing it — so the model must return a
schema-valid object. Lazy-imports the SDK (optional dep). Wrap in CassetteLLM so
dev/CI/eval replay for free; this only spends credits in record/live mode.
"""
from __future__ import annotations

import contextvars
import json
import os
import time
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from .llm import LLMResult

DEFAULT_MODEL = os.environ.get("ROSTER_LLM_MODEL", "claude-sonnet-5")

# Request-scoped per-call latency log (factra-style diagnostics). When a caller sets this to a fresh
# list, every complete() appends {model, max_tokens, in, out, ms} so the diagnostics trace can attribute
# wall-clock to individual LLM calls. Unset (None) → zero overhead, nothing recorded.
LLM_CALL_LOG: contextvars.ContextVar[list | None] = contextvars.ContextVar("llm_call_log", default=None)


def _recover_stringified(raw, model: type[BaseModel]) -> BaseModel | None:
    """Recover from a known model quirk: the model emits a container field (or the whole object) as a
    JSON *string* instead of a real list/object, so `model_validate` fails on an otherwise-correct
    answer. Runs ONLY after a real ValidationError, so the happy path is untouched. Returns a validated
    instance or None (caller then raises the original error). Observed on PanelPlan; general to any schema."""
    if not isinstance(raw, dict):
        return None
    # (a) un-stringify any field whose value is a JSON string ("[...]" or "{...}")
    coerced = {}
    for k, v in raw.items():
        if isinstance(v, str) and v.strip()[:1] in "[{":
            try:
                coerced[k] = json.loads(v)
            except (ValueError, TypeError):
                coerced[k] = v
        else:
            coerced[k] = v
    err0 = None
    try:
        return model.model_validate(coerced)
    except ValidationError as e:
        err0 = e   # keep it — the except-scoped name is cleared at block exit
    # (b) the model wrapped the WHOLE object as one field's (stringified) value — validate the inner dict
    for v in coerced.values():
        if isinstance(v, dict):
            try:
                return model.model_validate(v)
            except ValidationError:
                continue
    # (c) a COSMETIC enhancement field (reasoning-read / charts) came back malformed — e.g. the model bled
    # tool-call XML into `interpretation`. Drop ONLY those known-optional fields and salvage the answer;
    # never drop core payload (a broken PanelPlan.specialists still raises, not silently empties).
    pruned, changed = dict(coerced), False
    for err in err0.errors():
        loc = err.get("loc") or ()
        if loc and loc[0] in _DROPPABLE_FIELDS and loc[0] in pruned:
            pruned.pop(loc[0], None)
            changed = True
    if changed:
        try:
            return model.model_validate(pruned)
        except ValidationError:
            pass
    return None


# enhancement fields that must never sink an otherwise-valid answer if the model malforms them
_DROPPABLE_FIELDS = {"interpretation", "confidence", "charts", "reasoning_purpose", "reasoning_conclusion"}


class AnthropicLLM:
    def __init__(self, *, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self._model = model
        self._api_key = api_key
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            import anthropic
            # max_retries=5 (SDK default is 2): 529 overloaded bursts routinely outlast two
            # quick retries and then surface RAW to users ("Plain language: provider error
            # 529"). The SDK backs off exponentially per retry, so this trades a few seconds
            # of latency for riding out bursts — every call site (compose, layman, judges)
            # benefits. Real outages still fail after ~5 attempts and surface honestly.
            kw = {"max_retries": 5}
            self._client = anthropic.Anthropic(api_key=self._api_key, **kw) if self._api_key \
                else anthropic.Anthropic(**kw)

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
        # Anthropic keeps `system` separate; fold any system-role turns into it.
        sys_parts = [system] + [m["content"] for m in messages if m.get("role") == "system"]
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in messages if m.get("role") in ("user", "assistant")]

        tool = {
            "name": "emit",
            "description": "Emit the structured result for this step.",
            "input_schema": response_format.model_json_schema(),
        }
        import asyncio
        # The Anthropic SDK client here is SYNCHRONOUS; run it off the event loop so a research
        # request's LLM round-trip doesn't block the whole API (and SSE heartbeats can flush).
        _t0 = time.perf_counter()
        resp = await asyncio.to_thread(
            lambda: self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system="\n\n".join(p for p in sys_parts if p),
                messages=convo,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit"},
                **({"temperature": temperature} if temperature is not None else {}),
            ))
        _ms = int((time.perf_counter() - _t0) * 1000)   # per-call wall-clock (includes SDK retries/backoff)
        # A max_tokens cutoff truncates the emit tool-call mid-JSON, so block.input is a partial dict
        # that fails schema validation with an opaque pydantic error. Surface the REAL cause instead
        # (Rule 13) so the fix is "raise max_tokens", not a wild goose chase.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"Anthropic structured output truncated: hit max_tokens={max_tokens} before the "
                f"'{response_format.__name__}' emit call completed — raise max_tokens for this call.")
        parsed = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit":
                try:
                    parsed = response_format.model_validate(block.input)
                except ValidationError:
                    # the model sometimes stringifies a container field (or the whole object) — recover
                    # from that specific quirk rather than fail-safing an otherwise-correct answer.
                    parsed = _recover_stringified(block.input, response_format)
                    if parsed is None:
                        raise
                break
        if parsed is None:
            raise RuntimeError("Anthropic response contained no 'emit' tool_use block")

        _log = LLM_CALL_LOG.get()
        if _log is not None:
            _log.append({"model": self._model, "max_tokens": max_tokens, "ms": _ms,
                         "in": resp.usage.input_tokens, "out": resp.usage.output_tokens})
        return LLMResult(
            parsed=parsed,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cost_usd=Decimal(0),   # priced by the caller's cost governor if needed
            latency_ms=_ms,
            model=self._model,
        )
