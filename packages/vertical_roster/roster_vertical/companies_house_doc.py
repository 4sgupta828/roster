"""Turn a UK Companies House record into an official registry document + facets.

Companies House is the UK's statutory company registrar — the non-US analogue of an SEC/registry
filing. `source_kind=filing` → `evidence_kind.classify` returns `primary_filing` (the top,
audited-record tier), and `source_country=UK` fills the non-US-filings geography gap. The
`## Officers` section (directors/secretaries + roles + appointment dates) is the team/execution
signal — who actually runs the company, straight off the statutory register.

STRUCTURAL only (Rule 18): every field here is read verbatim off the registry record (company
number, status, incorporation date, officer names/roles) — no semantic judgment, no inference.
"""
from __future__ import annotations


def company_number(rec: dict) -> str:
    return str(rec.get("company_number") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("company_name") or rec.get("title") or "").strip()


def facets(rec: dict) -> dict:
    created = str(rec.get("date_of_creation") or "")
    f = {
        "source_kind": "filing",           # → primary_filing tier
        "source_country": "UK",
        "entity_type": "company",
        "company_status": str(rec.get("company_status") or "").strip(),
        "company_number": company_number(rec),
    }
    if len(created) >= 4 and created[:4].isdigit():
        f["year"] = created[:4]
    return {k: v for k, v in f.items() if v}


def _officers(rec: dict) -> list[dict]:
    raw = rec.get("officers")
    if isinstance(raw, dict):                 # a raw /officers response {items:[...]}
        raw = raw.get("items")
    return [o for o in (raw or []) if isinstance(o, dict)]


def to_markdown(rec: dict) -> str:
    name = title(rec) or "(unnamed company)"
    parts: list[str] = [f"# {name}", ""]

    meta: list[str] = []
    num = company_number(rec)
    if num:
        meta.append(f"Company number {num}")
    if rec.get("company_status"):
        meta.append(f"status: {rec['company_status']}")
    if rec.get("date_of_creation"):
        meta.append(f"incorporated {rec['date_of_creation']}")
    if rec.get("company_type"):
        meta.append(str(rec["company_type"]))
    lead = " · ".join(meta) if meta else "UK company"
    parts += [
        f"{lead}  (official UK Companies House registry record — the statutory company register)",
        "",
    ]

    ro = rec.get("registered_office_address")
    if isinstance(ro, dict):
        addr = ", ".join(
            str(ro[k]).strip()
            for k in ("address_line_1", "address_line_2", "locality", "region", "postal_code", "country")
            if ro.get(k)
        )
        if addr:
            parts += [f"Registered office: {addr}", ""]

    sic = rec.get("sic_codes")
    if isinstance(sic, list) and sic:
        parts += ["SIC codes: " + ", ".join(str(s) for s in sic if s), ""]

    officers = _officers(rec)
    if officers:
        parts += ["## Officers", ""]
        for o in officers:
            oname = str(o.get("name") or "").strip()
            if not oname:
                continue
            bits: list[str] = []
            role = str(o.get("officer_role") or "").strip()
            if role:
                bits.append(role)
            appointed = str(o.get("appointed_on") or "").strip()
            if appointed:
                bits.append(f"appointed {appointed}")
            resigned = str(o.get("resigned_on") or "").strip()
            if resigned:
                bits.append(f"resigned {resigned}")
            occ = str(o.get("occupation") or "").strip()
            if occ:
                bits.append(occ)
            suffix = f" — {'; '.join(bits)}" if bits else ""
            parts += [f"- {oname}{suffix}"]
        parts += [""]

    return "\n".join(parts).strip() + "\n"
