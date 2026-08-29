"""AnthropicLLM: a max_tokens cutoff must surface a CLEAR truncation error, not an opaque
pydantic ValidationError from a half-emitted tool call."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from roster_kernel.providers.anthropic_llm import AnthropicLLM
from roster_kernel.research.react import ComposedAnswer


class _FakeMessages:
    def __init__(self, resp):
        self._resp = resp

    def create(self, **kw):
        return self._resp


def _llm_with(resp):
    llm = AnthropicLLM(model="test", api_key="x")
    llm._client = SimpleNamespace(messages=_FakeMessages(resp))   # pre-set → _ensure() is a no-op
    return llm


def test_truncation_raises_clear_error():
    resp = SimpleNamespace(
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="tool_use", name="emit", input={})],  # partial/empty input
        usage=SimpleNamespace(input_tokens=10, output_tokens=2048))
    llm = _llm_with(resp)
    with pytest.raises(RuntimeError, match="truncated"):
        asyncio.run(llm.complete(system="s", messages=[{"role": "user", "content": "q"}],
                                 response_format=ComposedAnswer, max_tokens=2048))


def test_normal_completion_parses():
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="emit",
                                 input={"answer": "The answer [1].", "directly_addresses": True, "gap_note": ""})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    llm = _llm_with(resp)
    out = asyncio.run(llm.complete(system="s", messages=[{"role": "user", "content": "q"}],
                                   response_format=ComposedAnswer, max_tokens=8000))
    assert out.parsed.answer == "The answer [1]."
    assert out.output_tokens == 20


def test_recovers_whole_object_stringified_in_one_field():
    """Observed PanelPlan quirk: the model emits {"specialists": "<the whole JSON as a string>"} — the
    provider must recover it instead of fail-safing an otherwise-correct triage."""
    from roster_kernel.research.panel import PanelPlan
    inner = '{"specialists": [{"id": "cardiology", "rationale": "HF component"}]}'
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="emit", input={"specialists": inner})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    out = asyncio.run(_llm_with(resp).complete(system="s", messages=[{"role": "user", "content": "q"}],
                                               response_format=PanelPlan, max_tokens=1200))
    assert [s.id for s in out.parsed.specialists] == ["cardiology"]


def test_recovers_container_field_stringified():
    """The list field itself arrives as a JSON string: {"specialists": "[{...}]"} → un-stringify it."""
    from roster_kernel.research.panel import PanelPlan
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="emit",
                                 input={"specialists": '[{"id": "nephrology", "rationale": "CKD3"}]'})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    out = asyncio.run(_llm_with(resp).complete(system="s", messages=[{"role": "user", "content": "q"}],
                                               response_format=PanelPlan, max_tokens=1200))
    assert [s.id for s in out.parsed.specialists] == ["nephrology"]


def test_recovers_by_dropping_malformed_optional_field():
    """The model bled tool-call XML into the optional `interpretation` field — validation must not kill the
    whole answer; drop the malformed field and salvage the answer (the panel-synthesis failure)."""
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="emit", input={
            "answer": "The panel recommends X [1].", "directly_addresses": True, "gap_note": "",
            "interpretation": '\n<parameter name="kind">what_would_change_this'})],   # malformed str, not a list
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    out = asyncio.run(_llm_with(resp).complete(system="s", messages=[{"role": "user", "content": "q"}],
                                               response_format=ComposedAnswer, max_tokens=8000))
    assert out.parsed.answer == "The panel recommends X [1]."
    assert out.parsed.interpretation == []   # dropped → default


def test_unrecoverable_reraises_original():
    """A genuinely malformed input (not a stringified-container quirk) must still raise ValidationError."""
    from pydantic import ValidationError
    from roster_kernel.research.panel import PanelPlan
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="emit", input={"specialists": 42})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    with pytest.raises(ValidationError):
        asyncio.run(_llm_with(resp).complete(system="s", messages=[{"role": "user", "content": "q"}],
                                             response_format=PanelPlan, max_tokens=1200))
