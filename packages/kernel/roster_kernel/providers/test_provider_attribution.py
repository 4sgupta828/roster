"""CompositeWebSearch provider attribution: each merged hit records EVERY engine that returned its URL
(single engine ⇒ novel to it), so downstream can attribute cited evidence per search source."""
import asyncio

from roster_kernel.providers.websearch import CompositeWebSearch, WebResult


class _Stub:
    def __init__(self, results):
        self._r = results
    async def search(self, query, *, max_results=10, open_web=False, recency_days=None, max_chars=None):
        return list(self._r)


def test_composite_tags_providers_per_url():
    exa = _Stub([WebResult(url="https://a.com/x", title="A", snippet="", provider="exa"),
                 WebResult(url="https://shared.com/y", title="S", snippet="", provider="exa")])
    brave = _Stub([WebResult(url="https://b.com/z", title="B", snippet="", provider="brave"),
                   WebResult(url="https://shared.com/y", title="S", snippet="", provider="brave")])
    out = asyncio.run(CompositeWebSearch([exa, brave]).search("q"))
    by_url = {r.url: r for r in out}
    # a.com only from exa → novel to exa
    assert by_url["https://a.com/x"].providers == ("exa",)
    # b.com only from brave → novel to brave
    assert by_url["https://b.com/z"].providers == ("brave",)
    # shared.com returned by BOTH → not novel to either
    assert set(by_url["https://shared.com/y"].providers) == {"brave", "exa"}
    assert len(out) == 3          # deduped by url


def test_single_provider_hit_is_novel():
    exa = _Stub([WebResult(url="https://only.com/1", title="O", snippet="", provider="exa")])
    out = asyncio.run(CompositeWebSearch([exa]).search("q"))
    assert out[0].providers == ("exa",)


def test_hydration_fills_thin_result_in_place_no_dup():
    # Exa returns a FULL-text page; Brave returns a THIN snippet for a DIFFERENT (novel) url.
    exa = _Stub([WebResult(url="https://full.com/a", title="F", snippet="s", body="X"*2000, provider="exa")])
    brave = _Stub([WebResult(url="https://thin.com/b", title="T", snippet="s", body="short", provider="brave")])
    calls = {}
    async def hydrator(urls, q):
        calls["urls"] = list(urls)
        return {"https://thin.com/b": ("HYDRATED FULL TEXT " * 60, ("key highlight",))}
    out = asyncio.run(CompositeWebSearch([exa, brave], hydrator=hydrator, thin_chars=800).search("q"))
    by = {r.url: r for r in out}
    # only the THIN (brave) url was hydrated; the full exa page was NOT
    assert calls["urls"] == ["https://thin.com/b"]
    assert by["https://thin.com/b"].body.startswith("HYDRATED FULL TEXT")
    assert by["https://thin.com/b"].highlights == ("key highlight",)
    assert by["https://thin.com/b"].provider == "brave"          # discovery still credited to brave
    assert by["https://full.com/a"].body == "X"*2000            # exa untouched
    assert len(out) == 2                                         # NO new url added — no duplication


def test_shared_url_kept_as_fulltext_not_hydrated():
    # SAME url from Exa (full) and Brave (thin): dedup keeps Exa's full-text copy → not thin → not hydrated.
    exa = _Stub([WebResult(url="https://shared.com/x", title="S", snippet="", body="Y"*2000, provider="exa")])
    brave = _Stub([WebResult(url="https://shared.com/x", title="S", snippet="", body="tiny", provider="brave")])
    called = {"n": 0}
    async def hydrator(urls, q):
        called["n"] += 1
        return {}
    out = asyncio.run(CompositeWebSearch([exa, brave], hydrator=hydrator, thin_chars=800).search("q"))
    assert len(out) == 1 and out[0].body == "Y"*2000            # one url, exa full-text kept
    assert set(out[0].providers) == {"brave", "exa"}            # both credited for the url
    assert called["n"] == 0                                     # nothing thin → hydrator not even called
