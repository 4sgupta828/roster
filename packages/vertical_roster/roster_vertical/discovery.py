"""Discovery entity resolver — reads WHICH company/org a retrieved block is about.

Supplied to the kernel's aggregate_entities (the "who is working on X" scouting mechanic). Attributes:
- EDGAR filings (incl. Form D private issuers) → the filing's company (from the document title).
- GitHub repos → the owning org.
Papers/news are not attributed to a company in v1 (arXiv/OpenAlex tie to authors, not companies;
GDELT titles name a subject but not a clean canonical entity) — a later pass can LLM-extract those.
"""
from __future__ import annotations

import re

# filing_doc titles are "<COMPANY> — <FORM>"; strip the trailing form marker to get the company.
_FORM_SUFFIX = re.compile(r"\s*[—-]\s*(10-K|10-Q|S-1|DEF 14A|8-K|D)\s*$", re.IGNORECASE)


def entity_of(hit) -> "tuple[str, str] | None":
    src = getattr(hit, "source_key", "") or ""
    title = (getattr(hit, "document_title", "") or "").strip()
    if not src and getattr(hit, "document_id", ""):
        src = hit.document_id.split(":", 1)[0]

    if src in ("edgar", "sec"):
        name = _FORM_SUFFIX.sub("", title).strip()
        return (name, "company") if len(name) > 1 else None
    if src == "github":
        org = title.split("/", 1)[0].strip() if "/" in title else title
        return (org, "org") if len(org) > 1 else None
    # arXiv / OpenAlex / GDELT: no clean canonical company entity in v1.
    return None
