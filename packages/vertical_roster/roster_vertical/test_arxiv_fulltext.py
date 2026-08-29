"""arXiv full-text ingest (window param `fulltext`): OFF → title+abstract only (byte-identical to
before); ON → the whole paper body is appended (HTML-first). docling is the PDF fallback, imported
lazily — when HTML is unavailable AND docling is absent, it degrades to abstract-only (never crashes).
All offline: the fetch strategy is faked, so no network and no docling install needed."""
from __future__ import annotations

import asyncio

from .connectors import arxiv as arx

_PAPER = {"id": "2005.11401", "title": "Retrieval-Augmented Generation", "summary": "An abstract.",
          "authors": ["Lewis"], "categories": ["cs.CL"], "primary_category": "cs.CL",
          "published": "2020-05-22"}
_BIG_HTML = b"<html><body><h1>RAG</h1>" + b"<p>Introduction. Method. Experiments. Results. </p>" * 200 + b"</body></html>"


class _FakeFetch:
    """Returns big HTML for the html URL; raises for the pdf URL (simulating no HTML → PDF path)."""
    egress_class = "datacenter"
    def __init__(self, html=_BIG_HTML, html_ok=True):
        self._html, self._html_ok = html, html_ok
    async def fetch(self, url, **opts):
        if "/html/" in url:
            if self._html_ok:
                return self._html
            raise RuntimeError("no HTML available")
        if "/pdf/" in url:
            return b"%PDF-1.4 fake pdf bytes"
        raise RuntimeError("unexpected url " + url)


def _conn(**fake):
    c = arx.ArxivConnector(papers=[dict(_PAPER)])
    c.fetch_strategy = _FakeFetch(**fake)
    return c


def _fetch(c, window):
    ents = asyncio.run(c.discover_entities(window))
    docs = asyncio.run(c.list_documents(ents[0]))
    return asyncio.run(c.fetch_artifact(docs[0])).decode()


def test_default_is_fulltext():
    # arXiv is a depth source: no fulltext param → full body pulled (flag default ON). This prevents an
    # abstract-only re-ingest from clean-replacing an already-full-text paper down to 2 blocks.
    md = _fetch(_conn(), {})                          # no fulltext param
    assert "## Full text" in md
    assert "Introduction" in md and "Experiments" in md


def test_explicit_fulltext_false_opts_out_to_abstract():
    md = _fetch(_conn(), {"fulltext": "false"})       # explicit opt-out wins over the default
    assert "## Full text" not in md and "An abstract." in md
    assert len(md) < 500                              # abstract-sized


def test_flag_off_restores_abstract_default(monkeypatch):
    # ROSTER_ARXIV_FULLTEXT_DEFAULT=off → no-param jobs go back to abstract-only (reversible switch).
    monkeypatch.setattr(arx, "_FULLTEXT_DEFAULT", False)
    md = _fetch(_conn(), {})
    assert "## Full text" not in md and "An abstract." in md


def test_fulltext_appends_html_body():
    md = _fetch(_conn(), {"fulltext": "true"})
    assert "## Full text" in md
    body = md.split("## Full text", 1)[1]
    assert len(body) > 3000                           # the whole body, not the abstract
    assert "Introduction" in body and "Experiments" in body


def test_html_missing_and_docling_absent_degrades_to_abstract(monkeypatch):
    # HTML unavailable → PDF path → docling import fails (absent) → abstract-only, NO crash.
    import builtins
    real_import = builtins.__import__
    def _no_docling(name, *a, **k):
        if name.startswith("docling"):
            raise ModuleNotFoundError("No module named 'docling'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_docling)
    md = _fetch(_conn(html_ok=False), {"fulltext": "true"})
    assert "## Full text" not in md and "An abstract." in md   # graceful fallback


def test_docling_helper_is_the_only_pdf_parser():
    # the PDF fallback calls docling and nothing else (no pymupdf/fitz import in the parse path)
    import inspect
    src = inspect.getsource(arx._docling_pdf_to_markdown)
    assert "docling" in src and "fitz" not in src and "pymupdf" not in src.lower()
