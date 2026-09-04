"""Attachment → vision-ready images.

Turns user uploads (base64) into a small list of downscaled PNG/JPEG images the vision
pre-step can read. Handles standard images, PDF pages (rendered), and DICOM (windowed to
8-bit). Heavy deps (PyMuPDF, pydicom, Pillow, numpy) are imported lazily and per-type, so a
missing decoder degrades that ONE type with a note instead of breaking the API.

Everything is best-effort and defensive: a bad attachment yields a note, never an exception
that breaks /research. No attachment bytes are logged.
"""
from __future__ import annotations

import base64
import io

_ANTHROPIC_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_EDGE = 1400          # longest-edge px cap (Anthropic vision guidance ~1568)
_MAX_IMAGES = 4
_MAX_PDF_PAGES = 3
_MAX_DOC_CHARS = 12000    # cap uploaded-document text fed as context
_PDF_TEXT_MIN = 200       # a PDF with >= this many extracted chars is treated as a TEXT doc


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _png_from_pil(img) -> dict:
    """Downscale a PIL image to the edge cap and return a base64 PNG attachment dict."""
    from PIL import Image  # noqa: F401  (ensure PIL present)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, _MAX_EDGE / max(w, h)) if max(w, h) else 1.0
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return {"media_type": "image/png", "data": _b64(buf.getvalue())}


def _kind(att: dict) -> str:
    """Classify an attachment as 'image' | 'pdf' | 'dicom' | 'text' | 'unknown'."""
    mt = (att.get("media_type") or "").lower()
    name = (att.get("name") or "").lower()
    if mt.startswith("text/") or name.endswith((".txt", ".md")):
        return "text"                       # pasted text / plain-text upload → document context
    if mt in _ANTHROPIC_IMAGE_TYPES or mt.startswith("image/"):
        return "image"
    if mt == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mt in ("application/dicom", "application/dcm") or name.endswith(".dcm"):
        return "dicom"
    # magic-byte sniff for DICOM ("DICM" at byte 128) and PDF ("%PDF")
    try:
        raw = base64.b64decode(att.get("data", ""), validate=False)
        if raw[:4] == b"%PDF":
            return "pdf"
        if len(raw) > 132 and raw[128:132] == b"DICM":
            return "dicom"
        if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "image"
    except Exception:
        pass
    return "unknown"


def _image(att: dict) -> list[dict]:
    from PIL import Image
    raw = base64.b64decode(att["data"], validate=False)
    img = Image.open(io.BytesIO(raw))
    img.load()
    return [_png_from_pil(img)]


def _pdf_text(att: dict) -> str:
    """Extract the PDF's text layer (empty string if it's effectively a scanned image)."""
    import fitz  # PyMuPDF
    raw = base64.b64decode(att["data"], validate=False)
    chunks: list[str] = []
    with fitz.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            chunks.append(page.get_text())
            if sum(len(c) for c in chunks) > _MAX_DOC_CHARS:
                break
    return "\n".join(chunks).strip()[:_MAX_DOC_CHARS]


def attachment_texts(attachments: list[dict] | None) -> list[tuple[str, str]]:
    """The TEXT of each attachment a résumé could ride in — PDFs (text layer) and text/* files —
    as (name, text). No vision, no model: a résumé upload must work whether or not the vision
    pipeline is on. Unreadable / scanned files yield nothing (the caller says so)."""
    out: list[tuple[str, str]] = []
    for att in attachments or []:
        mt = str(att.get("media_type") or "").lower()
        name = str(att.get("name") or "")
        try:
            if mt == "application/pdf" or name.lower().endswith(".pdf"):
                txt = _pdf_text(att)
            elif mt.startswith("text/") or not mt:
                txt = base64.b64decode(att.get("data") or "", validate=False).decode("utf-8", "ignore")[:_MAX_DOC_CHARS]
            else:
                continue
        except Exception:  # noqa: BLE001 — a bad file is a note, never a failure
            continue
        if txt and txt.strip():
            out.append((name or mt or "file", txt.strip()))
    return out


def _pdf_images(att: dict) -> list[dict]:
    """Render the first pages of a (scanned/image) PDF to vision images."""
    import fitz  # PyMuPDF
    from PIL import Image
    raw = base64.b64decode(att["data"], validate=False)
    out: list[dict] = []
    with fitz.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            if len(out) >= _MAX_PDF_PAGES:
                break
            pix = page.get_pixmap(dpi=150)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            out.append(_png_from_pil(img))
    return out


def _dicom(att: dict) -> list[dict]:
    import numpy as np
    import pydicom
    from pydicom.pixel_data_handlers.util import apply_voi_lut
    from PIL import Image
    raw = base64.b64decode(att["data"], validate=False)
    ds = pydicom.dcmread(io.BytesIO(raw), force=True)
    arr = ds.pixel_array
    try:
        arr = apply_voi_lut(arr, ds)      # apply window/level if present
    except Exception:
        pass
    arr = arr.astype("float32")
    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        arr = arr.max() - arr             # invert so bone/dense reads bright
    lo, hi = float(arr.min()), float(arr.max())
    arr = (arr - lo) / (hi - lo) * 255.0 if hi > lo else arr * 0.0
    img = Image.fromarray(arr.astype("uint8"))
    return [_png_from_pil(img)]


def attachments_to_media(attachments: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Route each attachment to the right context channel:
      - images / scans → `images` [{media_type, data(base64 PNG)}] (vision),
      - plain text     → `docs` [{name, text}] (document context),
      - PDFs           → `pdfs` [{name, data, text_fallback}] — the RAW PDF, passed to the model as
        a NATIVE document block (SOTA parsing: the model sees the page layout, so lab-report tables
        keep their analyte/value/unit/range associations instead of the scrambled text layer that
        PyMuPDF's get_text() produces). `text_fallback` = the old extraction, used only if the
        native read fails. Oversized PDFs (> ~20 MB) fall back to text/pages with a note.
    Returns (images, docs, pdfs, notes); `notes` describe anything skipped. Never raises.
    """
    images: list[dict] = []
    docs: list[dict] = []
    pdfs: list[dict] = []
    notes: list[str] = []

    def _label(att):
        return att.get("name") or att.get("media_type") or "file"

    def _tag(ims, name):
        for im in ims:
            im["name"] = name
        return ims

    for att in attachments or []:
        if len(images) >= _MAX_IMAGES and _kind(att) not in ("pdf", "text"):
            notes.append("some attachments skipped (max 4 images per request)")
            break
        kind = _kind(att)
        try:
            if kind == "text":
                raw = base64.b64decode(att.get("data", ""), validate=False)
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    docs.append({"name": _label(att), "text": text[:_MAX_DOC_CHARS]})
            elif kind == "image":
                images.extend(_tag(_image(att), _label(att)))
            elif kind == "dicom":
                images.extend(_tag(_dicom(att), _label(att)))
            elif kind == "pdf":
                data = att.get("data", "")
                if len(data) <= 20_000_000 and len(pdfs) < 2:   # native read (max 2 PDFs/request)
                    pdfs.append({"name": _label(att), "data": data,
                                 "text_fallback": _pdf_text(att)})
                else:                                     # oversized/extra → old fallback paths
                    notes.append(f"'{_label(att)}' read via text extraction (size/count cap)")
                    text = _pdf_text(att)
                    if len(text) >= _PDF_TEXT_MIN:
                        docs.append({"name": _label(att), "text": text})
                    else:
                        images.extend(_tag(_pdf_images(att), _label(att)))
            else:
                notes.append(f"unsupported attachment '{_label(att)}'")
        except ModuleNotFoundError as e:
            notes.append(f"{kind} support unavailable ({e.name} not installed)")
        except Exception as e:  # noqa: BLE001 — never break the request on a bad file
            notes.append(f"couldn't read {kind} attachment ({type(e).__name__})")
    return images[:_MAX_IMAGES], docs, pdfs, notes


def _thumb(b64_png: str, edge: int = 220) -> str:
    """A tiny base64 PNG data-URI thumbnail of an already-decoded image (for session display)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64_png)))
        img.thumbnail((edge, edge))
        buf = io.BytesIO()
        (img.convert("RGB") if img.mode not in ("RGB", "L") else img).save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + _b64(buf.getvalue())
    except Exception:
        return ""


def session_previews(images: list[dict], docs: list[dict]) -> list[dict]:
    """Lightweight, displayable record of what the user uploaded — image thumbnails + doc
    names — small enough to store inline on the session (shown in Past Sessions)."""
    prev: list[dict] = []
    for im in images:
        prev.append({"name": im.get("name") or "image", "kind": "image", "thumb": _thumb(im["data"])})
    for d in docs:
        prev.append({"name": d.get("name") or "document", "kind": "document"})
    return prev
