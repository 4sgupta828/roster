"""Turn a podcast EPISODE (with a publisher-provided transcript) into a citable markdown document + facets.

This is the "human wisdom" surface: genuine expert/practitioner DISCUSSION — spoken opinion, not a peer-
reviewed finding. It grades a NEW `expert_analysis` tier (the orchestrator wires `source_kind="podcast"` →
that tier separately). It is perception/argument from named practitioners, so the answer format must present
it as recorded discussion, never as an established fact.

PROVENANCE / ToS (non-negotiable): we ONLY ever ingest the transcript the PUBLISHER provides (the
Podcasting-2.0 `<podcast:transcript>` element, or a VTT/SRT/plain/JSON transcript URL the feed itself
links). We NEVER machine-transcribe audio and NEVER scrape YouTube. An episode with no provided transcript
is skipped upstream (see connectors/podcast.py) — this module only ever sees text the publisher shipped.

STRUCTURAL only (Rule 18): normalize_transcript strips cue timings / indices and collapses whitespace —
that is text-format parsing, not a semantic judgment.
"""
from __future__ import annotations

import re


def episode_id(rec: dict) -> str:
    return str(rec.get("guid") or rec.get("id") or rec.get("link") or "").strip()


def title(rec: dict) -> str:
    return " ".join(str(rec.get("title") or "").split())


def show(rec: dict) -> str:
    return " ".join(str(rec.get("show") or rec.get("feed_title") or "").split())


def _year_from_pubdate(pub: str) -> str:
    # pubDate is RFC-822 ("Wed, 02 Mar 2025 10:00:00 GMT"); pull the 4-digit year (STRUCTURAL parse).
    m = re.search(r"(?:19|20)\d{2}", str(pub or ""))
    return m.group(0) if m else ""


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "podcast",          # → NEW "expert_analysis" tier (orchestrator wires classify)
        "source_country": "global",
        "entity_type": "podcast_episode",
        "show": show(rec),
    }
    yr = _year_from_pubdate(rec.get("pubDate") or rec.get("published") or "")
    if yr:
        f["year"] = yr
    return {k: v for k, v in f.items() if v}


# ---------------------------------------------------------------------------------------------------
# Transcript normalization: VTT / SRT / JSON (podcast-index segments) / plain → clean prose.
# ---------------------------------------------------------------------------------------------------
def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _strip_cues(text: str) -> str:
    """Drop WEBVTT header, cue-timing lines ('-->'), SRT indices / stray cue numbers, and inline
    caption tags (<v Host>, <00:00:00.000>, <c>). Handles BOTH VTT and SRT — the only structural
    difference (millisecond '.' vs ',' and the SRT index line) is absorbed by the same filters.
    """
    out: list[str] = []
    for raw_line in re.split(r"\r\n|\r|\n", text or ""):
        s = raw_line.strip()
        if not s:
            continue
        if s.upper().startswith("WEBVTT"):
            continue
        if "-->" in s:                       # cue timing line (VTT or SRT)
            continue
        if s.isdigit():                      # SRT block index / stray cue number
            continue
        if s.upper() in ("NOTE", "STYLE", "REGION"):
            continue
        s = re.sub(r"<[^>]+>", "", s).strip()   # inline caption / speaker tags
        if s:
            out.append(s)
    return _collapse(" ".join(out))


def _normalize_json(text: str) -> str | None:
    """podcast-index segment JSON: {"segments":[{startTime, body|text}]} or {"body":[...]} or a bare list.
    Returns None if the payload is not parseable segment JSON so the caller can fall through.
    """
    import json
    try:
        data = json.loads(text)
    except Exception:
        return None
    segs = None
    if isinstance(data, dict):
        segs = data.get("segments")
        if segs is None and isinstance(data.get("body"), list):
            segs = data.get("body")
    elif isinstance(data, list):
        segs = data
    if not isinstance(segs, list):
        return None
    parts: list[str] = []
    for seg in segs:
        if isinstance(seg, dict):
            t = seg.get("body") or seg.get("text") or ""
        else:
            t = str(seg)
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
    return _collapse(" ".join(parts))


def normalize_transcript(text: str, kind: str = "") -> str:
    """Normalize a publisher transcript of any supported format to clean prose."""
    if not text or not str(text).strip():
        return ""
    text = str(text)
    k = (kind or "").lower()

    if "json" in k or text.lstrip()[:1] in ("{", "["):
        prose = _normalize_json(text)
        if prose is not None:
            return prose
    if "vtt" in k or text.lstrip().upper().startswith("WEBVTT"):
        return _strip_cues(text)
    if "srt" in k or "subrip" in k or "-->" in text:
        return _strip_cues(text)
    # plain text (or html-wrapped plain): strip tags, collapse whitespace.
    return _collapse(re.sub(r"<[^>]+>", " ", text))


TRANSCRIPT_CHAR_CAP = 40000


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    date = str(rec.get("pubDate") or rec.get("published") or "").strip()
    meta = (
        f"{show(rec)} — podcast episode, {date}  "
        "(recorded expert/practitioner discussion — spoken opinion, publisher-provided transcript)"
    )
    parts += [meta.strip(), ""]
    link = str(rec.get("link") or "").strip()
    if link:
        parts += [f"Link: {link}", ""]
    turl = str(rec.get("transcript_url") or "").strip()
    if turl:
        parts += [f"Transcript: {turl}", ""]
    prose = normalize_transcript(rec.get("transcript_text") or "", rec.get("transcript_type") or "")
    if prose:
        parts += ["## Transcript", prose[:TRANSCRIPT_CHAR_CAP], ""]
    return "\n".join(parts).strip() + "\n"
