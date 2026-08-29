"""arXiv connector — preprint metadata (public API, Atom XML) + optional FULL TEXT.

discover_entities → query the arXiv API (or use injected fixture records); one EntityRef per
paper. list_documents → one synthesized markdown paper-document per arXiv id. fetch_artifact →
the assembled markdown bytes. Tests inject `papers` so they run offline.

FULL TEXT (opt-in, window param `fulltext`): by default we synthesize title+abstract only (a
paper = 2 blocks). With fulltext, fetch_artifact pulls the WHOLE paper body — HTML FIRST
(arxiv.org/html/{id}, clean LaTeXML text, best for tables/equations, no heavy deps) and DOCLING
as the PDF fallback (arxiv.org/pdf/{id}) for papers without HTML. The pipeline then chunks the
body into many blocks, so questions can ground in methods/results, not just the abstract. docling
is imported LAZILY — if it's absent the PDF fallback is skipped (HTML-only), never a crash.
"""
from __future__ import annotations

import logging
import os

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import paper_doc
from ._http import HttpStrategy

API = "https://export.arxiv.org/api/query"
HTML_URL = "https://arxiv.org/html/{id}"
PDF_URL = "https://arxiv.org/pdf/{id}"
_FULLTEXT_MIN_CHARS = 3000        # a real full body dwarfs an abstract; below this → not real full text
_log = logging.getLogger(__name__)

# arXiv is a DEPTH source: the product wants whole paper bodies, not abstracts. So full-text is the
# DEFAULT — a job pulls the full body UNLESS it explicitly opts out (`fulltext: false`). This also makes
# re-ingest NON-DEGRADING: the corpus clean-replaces a document's blocks on re-ingest (materialize.py
# A1), so an abstract-only re-ingest of an already-full-text paper would DELETE its body blocks. With
# full-text as the floor, breadth lanes (deeptech/recent) re-enrich rather than thin the same papers.
# Flag `ROSTER_ARXIV_FULLTEXT_DEFAULT` (default on) is the reversible switch back to abstract-default.
_FULLTEXT_DEFAULT = os.environ.get("ROSTER_ARXIV_FULLTEXT_DEFAULT", "true").lower() in ("1", "true", "yes")


def _want_fulltext(window: dict | None) -> bool:
    """Resolve full-text intent: explicit window['fulltext'] wins; absent → the flag default."""
    raw = (window or {}).get("fulltext")
    if raw is None:
        return _FULLTEXT_DEFAULT
    return str(raw).lower() in ("1", "true", "yes")


def _strip_html(raw: bytes) -> str:
    """arXiv HTML → plain text (stdlib only): drop script/style, unwrap tags, collapse whitespace."""
    import html as _html
    import re
    s = raw.decode("utf-8", "ignore")
    s = re.sub(r"(?is)<(script|style|nav|footer|header)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)          # drop remaining tags
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _docling_pdf_to_markdown(pdf_bytes: bytes) -> str:
    """Parse a PDF to markdown with DOCLING (and ONLY docling, per the ingest design). Imported
    lazily so the connector runs without docling installed — absence just disables the PDF fallback.
    Runs on a worker thread (docling is synchronous + heavy). Raises on failure (the caller catches).

    Uses factra's proven pipeline settings: docling for LAYOUT + TABLE STRUCTURE (TableFormer), NO OCR
    — arXiv PDFs are digital/text-bearing, so OCR adds large per-page cost and zero value (and the
    default OCR backend can be absent in a lean image). Parses from an in-memory stream (no temp file)."""
    import io
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opts = PdfPipelineOptions()
    opts.do_ocr = False                                    # text-bearing PDFs → no OCR
    opts.do_table_structure = True                         # keep tables (TableFormer)
    opts.table_structure_options.do_cell_matching = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    stream = DocumentStream(name="paper.pdf", stream=io.BytesIO(pdf_bytes))
    return (converter.convert(stream).document.export_to_markdown() or "").strip()


class ArxivConnector:
    key = "arxiv"

    def __init__(self, *, papers: list[dict] | None = None, page_size: int = 25):
        # arXiv rate-limits aggressively (429) and asks for ~3s between requests — use a patient
        # backoff so a burst of ingest jobs retries rather than dying.
        self.fetch_strategy = HttpStrategy(base_delay=3.0, max_retries=5)
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for p in (papers or []):
            self._by_id[paper_doc.arxiv_id(p)] = p

    @staticmethod
    def _parse_atom(raw: bytes) -> list[dict]:
        """Minimal Atom→record parse (stdlib only). Full-text ingest hardening is P2."""
        import xml.etree.ElementTree as ET
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        out: list[dict] = []
        root = ET.fromstring(raw)
        for e in root.findall("a:entry", ns):
            raw_id = (e.findtext("a:id", default="", namespaces=ns) or "").rsplit("/", 1)[-1]
            arxiv_id = raw_id.split("v")[0]
            cats = [c.get("term", "") for c in e.findall("a:category", ns)]
            prim = e.find("arxiv:primary_category", ns)
            out.append({
                "id": arxiv_id,
                "title": " ".join((e.findtext("a:title", default="", namespaces=ns) or "").split()),
                "authors": [a.findtext("a:name", default="", namespaces=ns) for a in e.findall("a:author", ns)],
                "categories": cats,
                "primary_category": prim.get("term", "") if prim is not None else (cats[0] if cats else ""),
                "published": (e.findtext("a:published", default="", namespaces=ns) or "")[:10],
                "summary": " ".join((e.findtext("a:summary", default="", namespaces=ns) or "").split()),
            })
        return out

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        # FULL-TEXT intent: default ON (arXiv is a depth source); explicit fulltext:false opts out.
        # Mark the cached records so fetch_artifact pulls the body — carried on the record (not a block
        # facet) so it survives discover→list→fetch within a job.
        _ft = _want_fulltext(window)
        if not (window or {}).get("query") and self._by_id:
            papers = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language models"
            limit = int((window or {}).get("limit", self._page_size))
            url = f"{API}?search_query=all:{q.replace(' ', '+')}&start=0&max_results={limit}"
            # freshness ingest lane: sort newest-first so the corpus gets RECENT preprints, not just the
            # most-cited older ones (default stays relevance). window {"sort":"recent"} opts in.
            if str((window or {}).get("sort", "")).lower() == "recent":
                url += "&sortBy=submittedDate&sortOrder=descending"
            papers = self._parse_atom(await self.fetch_strategy.fetch(url))
            for p in papers:
                self._by_id[paper_doc.arxiv_id(p)] = p
        if _ft:
            for p in papers:
                p["_fulltext"] = True
        return [EntityRef(source_key=self.key, native_id=paper_doc.arxiv_id(p),
                          title=paper_doc.title(p), facets=paper_doc.facets(p))
                for p in papers if paper_doc.arxiv_id(p)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=paper_doc.facets(p) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def _fulltext_body(self, arxiv_id: str) -> str:
        """The paper's full body — HTML first (clean, no heavy deps), docling PDF fallback. "" if neither."""
        # 1) arXiv HTML (LaTeXML). Real papers are large; a "no HTML available" stub is tiny → fall back.
        try:
            html = _strip_html(await self.fetch_strategy.fetch(HTML_URL.format(id=arxiv_id)))
            if len(html) >= _FULLTEXT_MIN_CHARS:
                return html
        except Exception as e:   # noqa: BLE001 — HTML missing/blocked → try the PDF path
            _log.info("arxiv %s: HTML fetch failed (%s) — trying PDF", arxiv_id, str(e)[:80])
        # 2) DOCLING on the PDF (lazy import; absent → skip, keep abstract-only). Runs off the loop.
        try:
            pdf = await self.fetch_strategy.fetch(PDF_URL.format(id=arxiv_id))
            import asyncio
            return await asyncio.to_thread(_docling_pdf_to_markdown, pdf)
        except Exception as e:   # noqa: BLE001 — no docling / parse failure → abstract-only fallback
            _log.info("arxiv %s: PDF/docling fallback failed (%s)", arxiv_id, str(e)[:80])
            return ""

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id)
        if p is None:
            url = f"{API}?id_list={doc.native_id}"
            recs = self._parse_atom(await self.fetch_strategy.fetch(url))
            p = recs[0] if recs else {"id": doc.native_id}
            self._by_id[doc.native_id] = p
        md = paper_doc.to_markdown(p)                          # title + abstract + metadata header
        if p.get("_fulltext"):
            body = await self._fulltext_body(paper_doc.arxiv_id(p) or doc.native_id)
            if body:
                md = md.rstrip() + "\n\n## Full text\n\n" + body
        return md.encode("utf-8")
