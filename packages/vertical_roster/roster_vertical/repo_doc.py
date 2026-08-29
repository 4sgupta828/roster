"""Turn a GitHub repository record into a citable markdown document + facets.

Open-source traction (stars/forks/activity) is a `technical_signal` — a real but SELF-REPORTED
adoption signal, never audited fact (the persona/authority tiers keep it below filings/papers). We
synthesize a doc from the repo metadata + its README (best-effort), so claims about a project's
approach or adoption are span-verifiable.
"""
from __future__ import annotations


def repo_id(rec: dict) -> str:
    return str(rec.get("full_name") or "").strip()


def title(rec: dict) -> str:
    return repo_id(rec) or str(rec.get("name") or "")


def facets(rec: dict) -> dict:
    owner = rec.get("owner") or {}
    f = {
        "source_kind": "code",
        "source_country": "global",
        "entity_type": "repository",
        "language": str(rec.get("language") or "").lower(),
        "org": str((owner.get("login") if isinstance(owner, dict) else owner) or "").lower(),
        "stars": str(rec.get("stargazers_count") or ""),
    }
    pushed = str(rec.get("pushed_at") or "")
    if pushed[:4].isdigit():
        f["year"] = pushed[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = []
    if rec.get("stargazers_count") is not None:
        meta.append(f"{rec['stargazers_count']:,} stars")
    if rec.get("forks_count") is not None:
        meta.append(f"{rec['forks_count']:,} forks")
    if rec.get("language"):
        meta.append(f"language: {rec['language']}")
    if rec.get("pushed_at"):
        meta.append(f"last push: {str(rec['pushed_at'])[:10]}")
    if meta:
        parts += ["## Traction", "; ".join(meta) + ".", ""]
    if rec.get("description"):
        parts += ["## Description", str(rec["description"]).strip(), ""]
    topics = rec.get("topics") or []
    if topics:
        parts += ["## Topics", ", ".join(str(t) for t in topics), ""]
    readme = str(rec.get("readme") or "").strip()
    if readme:
        parts += ["## README", readme[:24000], ""]
    return "\n".join(parts).strip() + "\n"
