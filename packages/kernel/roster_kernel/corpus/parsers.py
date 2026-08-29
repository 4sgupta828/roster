"""Content-type-keyed parser registry — domain-free.

Kernel ships plain-text + a stdlib HTML text-extractor (zero extra deps). Heavier
parsers (PDF via docling, XBRL for the financial vertical) register the same
`Parser` Protocol — the pipeline dispatches by `content_type`, so a vertical adds
a format without kernel edits.
"""
from __future__ import annotations

from html.parser import HTMLParser as _StdHTMLParser

from roster_kernel.contract.protocols import Parser


class ParserRegistry:
    def __init__(self) -> None:
        self._by_type: dict[str, Parser] = {}

    def register(self, parser: Parser) -> None:
        for ct in parser.content_types:
            self._by_type[ct] = parser

    def for_content_type(self, content_type: str) -> Parser:
        # exact, then prefix (e.g. "text/plain; charset=utf-8" → "text/plain")
        base = content_type.split(";", 1)[0].strip().lower()
        if base in self._by_type:
            return self._by_type[base]
        raise KeyError(f"no parser registered for content_type {content_type!r}")

    def supports(self, content_type: str) -> bool:
        return content_type.split(";", 1)[0].strip().lower() in self._by_type


class PlainTextParser:
    content_types = ("text/plain", "text/markdown")

    def parse(self, raw: bytes, *, content_type: str) -> str:
        return raw.decode("utf-8", errors="replace")


class _TextExtractor(_StdHTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):  # noqa: ANN001
        if not self._skip and data.strip():
            self._parts.append(data.strip())


class HtmlParser:
    content_types = ("text/html", "application/xhtml+xml")

    def parse(self, raw: bytes, *, content_type: str) -> str:
        ex = _TextExtractor()
        ex.feed(raw.decode("utf-8", errors="replace"))
        return "\n\n".join(ex._parts)


class PdfParser:
    """PDF → text/markdown, factra-style: **docling preferred** (layout/table-aware markdown — the
    quality that matters for guideline dosing tables), **PyMuPDF fallback** (fast plain-text extraction;
    already in the serve image for vision uploads). Engine picked at first use; `ROSTER_PDF_ENGINE`
    (auto|docling|pymupdf) forces one if docling misbehaves in an environment. Docling downloads its
    models on first conversion — the first PDF ingest in a fresh container is slow (minutes); that cost
    is per-container, not per-document."""
    content_types = ("application/pdf",)

    def __init__(self) -> None:
        self._docling = None          # lazily-built DocumentConverter (model load once per process)

    @staticmethod
    def _engine() -> str:
        import os
        return os.environ.get("ROSTER_PDF_ENGINE", "auto").strip().lower()

    def _parse_docling(self, raw: bytes) -> str:
        from io import BytesIO
        from docling.datamodel.base_models import DocumentStream
        from docling.document_converter import DocumentConverter
        if self._docling is None:
            self._docling = DocumentConverter()
        res = self._docling.convert(DocumentStream(name="doc.pdf", stream=BytesIO(raw)))
        return res.document.export_to_markdown()

    @staticmethod
    def _parse_pymupdf(raw: bytes) -> str:
        import fitz   # PyMuPDF
        with fitz.open(stream=raw, filetype="pdf") as doc:
            return "\n\n".join(page.get_text("text") for page in doc)

    def parse(self, raw: bytes, *, content_type: str) -> str:
        eng = self._engine()
        if eng in ("auto", "docling"):
            try:
                return self._parse_docling(raw)
            except ImportError:
                if eng == "docling":
                    raise
            except Exception:
                # docling failed on THIS document (or model download failed) → fall through to PyMuPDF
                if eng == "docling":
                    raise
        return self._parse_pymupdf(raw)


def _pdf_available() -> bool:
    """Register the PDF parser only when an engine is importable — otherwise PDFs stay unsupported
    (exactly today's behavior; nothing new can break)."""
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import docling  # noqa: F401
        return True
    except ImportError:
        return False


def default_registry() -> ParserRegistry:
    reg = ParserRegistry()
    reg.register(PlainTextParser())
    reg.register(HtmlParser())
    if _pdf_available():
        reg.register(PdfParser())
    return reg
