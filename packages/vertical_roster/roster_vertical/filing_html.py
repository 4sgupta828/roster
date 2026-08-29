"""Parse a filing's primary-document HTML into named narrative sections (STRUCTURAL, Rule 18).

A 10-K/10-Q is organized by numbered Items ("Item 1. Business", "Item 1A. Risk Factors",
"Item 7. Management's Discussion…"). We locate those markers by regex (a computable structure, not a
semantic judgment) and take, for each item, its LARGEST text span (the table-of-contents entry is a
tiny span; the real section is large). S-1/other prospectuses use caption headers, handled as a
fallback. Everything is length-capped so we never embed a whole 100k-char filing.
"""
from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_MULTINL = re.compile(r"\n\s*\n\s*\n+")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

# "Item 1.", "Item 1A.", "Item 7.:" etc. (case-insensitive), start-of-lineish.
_ITEM = re.compile(r"\bItem\s+(\d{1,2}[A-Z]?)\s*[.\:\-–]", re.IGNORECASE)

_ITEM_TITLES = {
    "1": "Business", "1A": "Risk Factors", "1B": "Unresolved Staff Comments",
    "2": "Properties", "3": "Legal Proceedings",
    "7": "Management's Discussion and Analysis", "7A": "Market Risk Disclosures",
    "8": "Financial Statements", "9A": "Controls and Procedures",
}
# The narrative items worth ingesting for diligence (skip boilerplate/financial-statement dumps).
_WANTED_ITEMS = {"1", "1A", "3", "7", "7A"}

# Caption headers worth extracting across form types (S-1 prospectus, 10-K notes, DEF 14A proxy).
# Diligence-relevant: prospectus narrative, SEGMENT + GEOGRAPHIC revenue disaggregation, and the
# PEOPLE layer (executive officers / compensation / board) found in proxies.
_CAPTION_HEADERS = [
    # prospectus / narrative
    "PROSPECTUS SUMMARY", "RISK FACTORS", "MANAGEMENT'S DISCUSSION AND ANALYSIS",
    "BUSINESS", "USE OF PROCEEDS",
    # segment + geographic revenue (customer/geo-segmented revenue the diligence needs)
    "SEGMENT INFORMATION", "SEGMENT REPORTING", "REPORTABLE SEGMENTS",
    "DISAGGREGATION OF REVENUE", "REVENUE BY GEOGRAPHIC", "GEOGRAPHIC INFORMATION",
    "REVENUE BY GEOGRAPHY", "INFORMATION ABOUT GEOGRAPHIC AREAS", "CONCENTRATION OF",
    # people (DEF 14A proxy)
    "EXECUTIVE OFFICERS", "EXECUTIVE COMPENSATION", "COMPENSATION DISCUSSION AND ANALYSIS",
    "BOARD OF DIRECTORS", "DIRECTORS AND EXECUTIVE OFFICERS", "SECURITY OWNERSHIP",
    "NOMINEES FOR DIRECTOR", "INFORMATION ABOUT OUR EXECUTIVE OFFICERS",
]

_MAX_SECTION = 14000     # cap one section (splitter chunks further at 8k) — smaller so more sections fit
_MAX_TOTAL = 80000       # cap total narrative per filing (embedding-cost guard)

# Diligence priority: keep these sections FIRST within the total cap (so MD&A + segment/geo revenue
# aren't squeezed out by a huge Business/Risk section). Lower rank = kept earlier. Matched by substring.
_SECTION_PRIORITY = [
    "Management's Discussion", "Financial Highlights", "Reportable Segments", "Segment Information",
    "Segment Reporting", "Disaggregation Of Revenue", "Revenue By Geographic", "Geographic Information",
    "Revenue By Geography", "Information About Geographic", "Risk Factors", "Business", "Concentration Of",
    "Executive Compensation", "Executive Officers", "Compensation Discussion", "Board Of Directors",
    "Directors And Executive", "Security Ownership", "Nominees For Director", "Prospectus Summary",
]


def _priority_rank(title: str) -> int:
    for i, key in enumerate(_SECTION_PRIORITY):
        if key.lower() in title.lower():
            return i
    return len(_SECTION_PRIORITY)


def html_to_text(raw: bytes) -> str:
    s = raw.decode("utf-8", "ignore")
    s = _SCRIPT_STYLE.sub(" ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = _MULTINL.sub("\n\n", s)
    return s.strip()


def _item_sections(text: str) -> dict[str, str]:
    """For each wanted Item, its largest text span between consecutive item markers."""
    marks = [(m.group(1).upper(), m.start()) for m in _ITEM.finditer(text)]
    if len(marks) < 3:
        return {}
    spans: dict[str, str] = {}
    for i, (num, pos) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        body = text[pos:end].strip()
        if num in _WANTED_ITEMS and len(body) > len(spans.get(num, "")):
            spans[num] = body[:_MAX_SECTION]
    return {_ITEM_TITLES.get(k, f"Item {k}"): v for k, v in spans.items() if len(v) > 200}


def _caption_sections(text: str) -> dict[str, str]:
    """Extract caption-header sections (S-1 narrative, 10-K segment/geo notes, DEF 14A people).

    Robust to tables-of-contents: for each header, use its LARGEST span (a ToC entry is a tiny span;
    the real section is large). All occurrences of all headers are sorted and each span runs to the
    next header occurrence."""
    up = text.upper()
    occ: list[tuple[str, int]] = []
    for cap in _CAPTION_HEADERS:
        start = 0
        while True:
            idx = up.find(cap, start)
            if idx == -1:
                break
            occ.append((cap.title(), idx))
            start = idx + len(cap)
    occ.sort(key=lambda x: x[1])
    out: dict[str, str] = {}
    for i, (title, pos) in enumerate(occ):
        end = occ[i + 1][1] if i + 1 < len(occ) else min(len(text), pos + _MAX_SECTION)
        body = text[pos:end].strip()
        if len(body) > 400 and len(body) > len(out.get(title, "")):   # keep the largest span per title
            out[title] = body[:_MAX_SECTION]
    return out


def sections_from_html(raw: bytes) -> dict[str, str]:
    """Return {section_title: text} for a filing's primary HTML, length-capped, best-effort.

    Merges numbered-Item sections (10-K/10-Q) with caption sections (segment/geo notes, proxy people),
    so a 10-K yields Business/Risk/MD&A PLUS its segment & geographic-revenue disclosures, and a
    DEF 14A yields the executive/board sections. Item titles win on a name collision."""
    text = html_to_text(raw)
    secs = {**_caption_sections(text), **_item_sections(text)}
    if not secs:
        # last resort: the leading narrative (skip a short cover page), capped.
        secs = {"Filing Text": text[:_MAX_TOTAL]}
    # enforce a total cap, keeping DILIGENCE-PRIORITY sections first (MD&A, segment/geo before a
    # giant Business/Risk block), then by size.
    total, kept = 0, {}
    for title, body in sorted(secs.items(), key=lambda kv: (_priority_rank(kv[0]), -len(kv[1]))):
        if total >= _MAX_TOTAL:
            break
        room = _MAX_TOTAL - total
        kept[title] = body[:room]
        total += len(kept[title])
    return kept
