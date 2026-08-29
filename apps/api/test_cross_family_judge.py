"""Cross-family judge (ROSTER_CROSS_FAMILY_JUDGE): flag + OPENAI_API_KEY → service.derive_judge_llm.

Locks: flag ON + a key → derive_judge_llm is an OpenAILLMClient (a DIFFERENT-family judge);
flag OFF, or ON with NO key → derive_judge_llm is None (today's behavior, byte-identical). The
OpenAILLMClient is LAZY (no SDK import / no connection at construction), so a fake key suffices —
no network, no mock needed for construction.
"""
from __future__ import annotations

import os

from roster_kernel.providers.openai_client import OpenAILLMClient


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def _set(flag: str, key: str | None) -> None:
    os.environ["ROSTER_CROSS_FAMILY_JUDGE"] = flag
    if key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = key


def test_flag_on_with_key_wires_openai_judge() -> None:
    prior_flag = os.environ.get("ROSTER_CROSS_FAMILY_JUDGE", "")
    prior_key = os.environ.get("OPENAI_API_KEY")
    try:
        _set("1", "sk-fake-test-key")
        a = _reload_app()
        assert a.cross_family_judge_enabled() is True
        judge = a.build_default_service().derive_judge_llm
        assert isinstance(judge, OpenAILLMClient)
    finally:
        os.environ["ROSTER_CROSS_FAMILY_JUDGE"] = prior_flag
        if prior_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = prior_key
        _reload_app()


def test_flag_off_gives_none() -> None:
    prior_flag = os.environ.get("ROSTER_CROSS_FAMILY_JUDGE", "")
    prior_key = os.environ.get("OPENAI_API_KEY")
    try:
        _set("", "sk-fake-test-key")   # OFF even WITH a key → None (byte-identical)
        a = _reload_app()
        assert a.cross_family_judge_enabled() is False
        assert a.build_default_service().derive_judge_llm is None
    finally:
        os.environ["ROSTER_CROSS_FAMILY_JUDGE"] = prior_flag
        if prior_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = prior_key
        _reload_app()


def test_flag_on_without_key_gives_none() -> None:
    prior_flag = os.environ.get("ROSTER_CROSS_FAMILY_JUDGE", "")
    prior_key = os.environ.get("OPENAI_API_KEY")
    try:
        _set("1", None)   # ON but NO key → fail-safe to None
        a = _reload_app()
        assert a.build_default_service().derive_judge_llm is None
    finally:
        os.environ["ROSTER_CROSS_FAMILY_JUDGE"] = prior_flag
        if prior_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = prior_key
        _reload_app()
