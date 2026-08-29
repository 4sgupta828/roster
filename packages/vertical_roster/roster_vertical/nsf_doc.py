"""Turn an NSF Awards record (api.nsf.gov) into a citable markdown grant document + facets.

An NSF award is a GOVERNMENT FUNDING record — who was funded to build what. `source_kind=funding`
→ `evidence_kind.classify` returns `verified_structured` (a step above press/preprints): a real,
attested funding signal useful for whitespace/opportunity spotting. STRUCTURAL only (Rule 18):
awardee/PI/amount/year are fields we read off the record, not a judgment about meaning.
"""
from __future__ import annotations


def award_id(rec: dict) -> str:
    return str(rec.get("id") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or "").strip()


def _pi(rec: dict) -> str:
    first = str(rec.get("piFirstName") or "").strip()
    last = str(rec.get("piLastName") or "").strip()
    return " ".join(p for p in (first, last) if p)


def _amount(rec: dict) -> str:
    return str(rec.get("fundsObligatedAmt") or "").strip()


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "funding",
        "source_country": "US",
        "entity_type": "grant",
        "agency": "NSF",
        "awardee": str(rec.get("awardeeName") or "").strip(),
        "amount": _amount(rec),
    }
    start = str(rec.get("startDate") or "").strip()
    if len(start) >= 4 and start[:4].isdigit():
        f["year"] = start[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = [f"NSF award {award_id(rec)}" if award_id(rec) else "NSF award"]
    if rec.get("awardeeName"):
        meta.append(f"awardee: {str(rec['awardeeName']).strip()}")
    if _pi(rec):
        meta.append(f"PI: {_pi(rec)}")
    if _amount(rec):
        meta.append(f"${_amount(rec)} obligated")
    if rec.get("startDate"):
        meta.append(f"start {str(rec['startDate']).strip()}")
    parts += [" · ".join(meta)
              + "  (US government grant record — verified funding signal, not a market claim)", ""]
    ab = str(rec.get("abstractText") or "").strip()
    if ab:
        import re
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ab)).strip()
        if clean:
            parts += ["## Abstract", clean, ""]
    return "\n".join(parts).strip() + "\n"
