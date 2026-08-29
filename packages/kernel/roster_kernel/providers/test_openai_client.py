"""Unit tests for OpenAILLMClient — NO network. A fake AsyncOpenAI client is injected directly
(`_client`), so `_ensure()` never imports/constructs the real SDK. Covers the json_schema primary
path and the json_object fallback (first create raises → retry succeeds)."""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from roster_kernel.providers.openai_client import OpenAILLMClient


class _Small(BaseModel):
    unsupported: list[str] = []


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content: str, *, prompt_tokens: int = 11, completion_tokens: int = 7):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, *, payload: str, fail_first: bool = False):
        self._payload = payload
        self._fail_first = fail_first
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_first and len(self.calls) == 1:
            raise RuntimeError("json_schema mode not supported by this model")
        return _FakeResponse(self._payload)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeAsyncOpenAI:
    def __init__(self, completions: _FakeCompletions):
        self.chat = _FakeChat(completions)


def _make_client(*, fail_first: bool = False, payload: str | None = None) -> tuple[OpenAILLMClient, _FakeCompletions]:
    payload = payload if payload is not None else json.dumps({"unsupported": ["span one", "span two"]})
    comps = _FakeCompletions(payload=payload, fail_first=fail_first)
    c = OpenAILLMClient(model="gpt-4o-test", api_key="sk-fake")
    c._client = _FakeAsyncOpenAI(comps)   # inject → _ensure() is a no-op, no SDK import
    return c, comps


@pytest.mark.asyncio
async def test_json_schema_primary_path():
    c, comps = _make_client()
    res = await c.complete(
        system="You are a judge.",
        messages=[{"role": "user", "content": "grade this"}],
        response_format=_Small,
    )
    # parsed is a validated instance of the response_format model
    assert isinstance(res.parsed, _Small)
    assert res.parsed.unsupported == ["span one", "span two"]
    assert res.output_tokens == 7
    assert res.input_tokens == 11
    assert res.model == "gpt-4o-test"
    # exactly one call, and it used json_schema response_format
    assert len(comps.calls) == 1
    assert comps.calls[0]["response_format"]["type"] == "json_schema"
    assert comps.calls[0]["response_format"]["json_schema"]["strict"] is False


@pytest.mark.asyncio
async def test_json_object_fallback_path():
    c, comps = _make_client(fail_first=True)
    res = await c.complete(
        system="You are a judge.",
        messages=[{"role": "user", "content": "grade this"}],
        response_format=_Small,
    )
    assert isinstance(res.parsed, _Small)
    assert res.parsed.unsupported == ["span one", "span two"]
    assert res.output_tokens == 7
    # two calls: json_schema (raised) then json_object fallback
    assert len(comps.calls) == 2
    assert comps.calls[0]["response_format"]["type"] == "json_schema"
    assert comps.calls[1]["response_format"] == {"type": "json_object"}
    # fallback describes the schema in the (folded) system prompt
    fb_system = comps.calls[1]["messages"][0]
    assert fb_system["role"] == "system"
    assert "JSON Schema" in fb_system["content"]


@pytest.mark.asyncio
async def test_defaulted_partial_object_parses():
    # empty object → defaulted field (non-strict schema must allow this)
    c, _ = _make_client(payload=json.dumps({}))
    res = await c.complete(
        system="s", messages=[{"role": "user", "content": "q"}], response_format=_Small,
    )
    assert isinstance(res.parsed, _Small)
    assert res.parsed.unsupported == []


@pytest.mark.asyncio
async def test_system_role_messages_folded():
    c, comps = _make_client()
    await c.complete(
        system="base",
        messages=[
            {"role": "system", "content": "extra sys"},
            {"role": "user", "content": "q"},
        ],
        response_format=_Small,
    )
    msgs = comps.calls[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert "base" in msgs[0]["content"] and "extra sys" in msgs[0]["content"]
    # user turn preserved; the system-role turn is NOT left as a separate message
    assert [m["role"] for m in msgs] == ["system", "user"]
