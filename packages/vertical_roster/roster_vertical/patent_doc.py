"""Turn a PatentsView patent record into a citable markdown document + facets.

A GRANTED patent is a legal record → `primary_filing` tier (via evidence_kind: source_kind=patent +
grant_status=granted). A pending application is intent, not fact → `technical_signal`. We read the
declared grant status; we never infer it (Rule 18).
"""
from __future__ import annotations


def patent_id(rec: dict) -> str:
    return str(rec.get("patent_id") or rec.get("document_number") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("patent_title") or rec.get("invention_title") or "").strip()


def _assignee(rec: dict) -> str:
    for a in (rec.get("assignees") or []):
        if isinstance(a, dict):
            org = a.get("assignee_organization") or a.get("assignee_organization_name")
            if org:
                return str(org)
    return ""


def facets(rec: dict, *, granted: bool = True) -> dict:
    date = str(rec.get("patent_date") or rec.get("filing_date") or "")
    cpc = ""
    for c in (rec.get("cpc_current") or rec.get("cpcs") or []):
        if isinstance(c, dict):
            cpc = str(c.get("cpc_group_id") or c.get("cpc_subclass_id") or "")
            if cpc:
                break
    f = {
        "source_kind": "patent",
        "source_country": "US",
        "entity_type": "patent",
        "grant_status": "granted" if granted else "application",
        "assignee": _assignee(rec).lower(),
        "cpc": cpc.lower(),
    }
    if date[:4].isdigit():
        f["year"] = date[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict, *, granted: bool = True) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    hdr = f"Patent {patent_id(rec)} — {'GRANTED' if granted else 'APPLICATION'}"
    if rec.get("patent_date"):
        hdr += f", dated {str(rec['patent_date'])[:10]}"
    parts += [hdr, ""]
    if _assignee(rec):
        parts += ["## Assignee", _assignee(rec), ""]
    if rec.get("patent_abstract"):
        parts += ["## Abstract", str(rec["patent_abstract"]).strip(), ""]
    return "\n".join(parts).strip() + "\n"
