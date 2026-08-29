"""T1 — Contract `subject_kind` (ROSTER_WEB_ENTITY_OPEN): opaque LLM subject judgment.

Covers (a) the kernel VALIDATION logic in `derive_contract` — a scripted fake LLM emits each of
{specific_entity, general, bogus, ""} and we assert the known classes survive, anything else → "";
(b) `Contract` carries the field; (c) the flag-gated manifest prompt swap is a true no-op when OFF
(byte-identical to TECH_CONTRACT_PROMPT) and the entity variant when ON.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from types import SimpleNamespace

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.contract import Contract, derive_contract

_PROMPT = "Derive the contract."


class _ScriptedLLM:
    """Returns one pre-scripted parsed object (duck-typed like the pydantic parse)."""
    def __init__(self, parsed):
        self._parsed = parsed

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._parsed, output_tokens=5, model="scripted")


def _derive(**out_fields) -> Contract | None:
    out = SimpleNamespace(mode="exploratory", entities=[], axes=[], **out_fields)
    return asyncio.run(derive_contract("q?", _ScriptedLLM(out), _PROMPT))


# ---- Contract carries the field --------------------------------------------------------------

def test_contract_default_subject_kind_is_empty():
    assert Contract(mode="exploratory").subject_kind == ""


def test_contract_carries_subject_kind():
    assert Contract(mode="exploratory", subject_kind="specific_entity").subject_kind == "specific_entity"


# ---- derive_contract validation of subject_kind ----------------------------------------------

def test_derive_keeps_specific_entity():
    c = _derive(subject_kind="specific_entity")
    assert c is not None and c.subject_kind == "specific_entity"


def test_derive_keeps_general():
    c = _derive(subject_kind="general")
    assert c is not None and c.subject_kind == "general"


def test_derive_normalizes_case_and_whitespace():
    c = _derive(subject_kind="  Specific_Entity ")
    assert c is not None and c.subject_kind == "specific_entity"


def test_derive_bogus_subject_kind_degrades_to_empty():
    c = _derive(subject_kind="bogus")
    assert c is not None and c.subject_kind == ""


def test_derive_missing_subject_kind_is_empty():
    # a prompt that never asks for the field → the parse has no attribute → "" (OFF byte-identical)
    c = asyncio.run(derive_contract("q?", _ScriptedLLM(
        SimpleNamespace(mode="exploratory", entities=[], axes=[])), _PROMPT))
    assert c is not None and c.subject_kind == ""


# ---- flag-gated manifest prompt swap (Rule 20) -----------------------------------------------

def _reload_manifest_module():
    # manifest reads the flag at build time via web_entity_open_on(); reload to re-read os.environ.
    # NOTE: roster_vertical/__init__ shadows the `manifest` attribute with a built instance, so
    # `import roster_vertical.manifest as m` yields the instance — fetch the real module module.
    import sys
    importlib.import_module("roster_vertical.manifest")
    return importlib.reload(sys.modules["roster_vertical.manifest"])


def test_manifest_prompt_off_is_original_identity(monkeypatch=None):
    from roster_vertical.answer_contract import TECH_CONTRACT_PROMPT
    os.environ.pop("ROSTER_WEB_ENTITY_OPEN", None)
    m = _reload_manifest_module()
    assert m.web_entity_open_on() is False
    assert m.build_manifest().contract_prompt == TECH_CONTRACT_PROMPT   # byte-identical OFF


def test_manifest_prompt_on_uses_entity_variant():
    from roster_vertical.answer_contract import (TECH_CONTRACT_PROMPT,
                                                     TECH_CONTRACT_PROMPT_ENTITY)
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = "1"
    try:
        m = _reload_manifest_module()
        assert m.web_entity_open_on() is True
        cp = m.build_manifest().contract_prompt
        assert cp == TECH_CONTRACT_PROMPT_ENTITY
        assert cp != TECH_CONTRACT_PROMPT
        assert cp.startswith(TECH_CONTRACT_PROMPT)          # entity variant extends the base
        assert "subject_kind" in cp
    finally:
        os.environ.pop("ROSTER_WEB_ENTITY_OPEN", None)
        _reload_manifest_module()                            # restore module to OFF state
