from __future__ import annotations

import asyncio
from types import SimpleNamespace

from roster_kernel.providers.exa_web import ExaWebSearch


def test_exa_defaults_preserve_existing_payload(monkeypatch) -> None:
    seen: dict = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": []}

    class Client:
        def __init__(self, *, timeout):
            self.timeout = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def post(self, url, *, json, headers):
            seen.update(json)
            return Resp()

    monkeypatch.setitem(
        __import__("sys").modules,
        "httpx",
        SimpleNamespace(
            AsyncClient=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            HTTPError=type("HTTPError", (Exception,), {}),
        ),
    )

    asyncio.run(ExaWebSearch(api_key="k").search("q"))

    assert seen["numResults"] == 8
    assert seen["contents"]["text"]["maxCharacters"] == 4000


def test_exa_accepts_raised_results_and_text_cap(monkeypatch) -> None:
    seen: dict = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": []}

    class Client:
        def __init__(self, *, timeout):
            self.timeout = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def post(self, url, *, json, headers):
            seen.update(json)
            return Resp()

    monkeypatch.setitem(
        __import__("sys").modules,
        "httpx",
        SimpleNamespace(
            AsyncClient=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            HTTPError=type("HTTPError", (Exception,), {}),
        ),
    )

    asyncio.run(ExaWebSearch(api_key="k").search("q", max_results=18, max_chars=12000))

    assert seen["numResults"] == 18
    assert seen["contents"]["text"]["maxCharacters"] == 12000
