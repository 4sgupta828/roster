"""Turn an NIH RePORTER project record (api.reporter.nih.gov) into a markdown grant document + facets.

An NIH RePORTER project is a GOVERNMENT FUNDING record — who was funded to build what.
`source_kind=funding` → `evidence_kind.classify` returns `verified_structured` (a step above
press/preprints): an attested funding signal useful for whitespace/opportunity spotting. STRUCTURAL
only (Rule 18): org/PI/amount/fiscal-year are fields we read off the record, not a semantic judgment.
"""
from __future__ import annotations


def project_id(rec: dict) -> str:
    return str(rec.get("project_num") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("project_title") or "").strip()


def _org(rec: dict) -> str:
    org = rec.get("organization")
    if isinstance(org, dict):
        return str(org.get("org_name") or "").strip()
    return ""


def _pis(rec: dict) -> list[str]:
    out: list[str] = []
    for pi in (rec.get("principal_investigators") or []):
        if isinstance(pi, dict):
            nm = str(pi.get("full_name") or "").strip()
            if nm:
                out.append(nm)
    return out


def _amount(rec: dict) -> str:
    amt = rec.get("award_amount")
    return str(amt).strip() if amt is not None else ""


def _year(rec: dict) -> str:
    fy = rec.get("fiscal_year")
    return str(fy).strip() if fy is not None and str(fy).strip().isdigit() else ""


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "funding",
        "source_country": "US",
        "entity_type": "grant",
        "agency": "NIH",
        "awardee": _org(rec),
        "amount": _amount(rec),
        "year": _year(rec),
    }
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = [f"NIH RePORTER project {project_id(rec)}" if project_id(rec) else "NIH RePORTER project"]
    if _org(rec):
        meta.append(f"organization: {_org(rec)}")
    if _pis(rec):
        meta.append(f"PI: {', '.join(_pis(rec))}")
    if _amount(rec):
        meta.append(f"${_amount(rec)} awarded")
    if _year(rec):
        meta.append(f"FY{_year(rec)}")
    parts += [" · ".join(meta)
              + "  (US government grant record — verified funding signal, not a market claim)", ""]
    ab = str(rec.get("abstract_text") or "").strip()
    if ab:
        import re
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ab)).strip()
        if clean:
            parts += ["## Abstract", clean, ""]
    return "\n".join(parts).strip() + "\n"
