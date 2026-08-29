"""Turn a Hugging Face Hub model record into a citable markdown document + facets.

Model adoption on the Hub (downloads/likes) is a `technical_signal` — a real but SELF-REPORTED
adoption signal, never audited fact (the persona/authority tiers keep it below filings/papers).
Download and like counts are publisher/community-driven metrics we read off the Hub API, so claims
about a model's traction or task are span-verifiable against the synthesized card. STRUCTURAL only
(Rule 18): downloads/likes are engagement counts, not a judgment of a model's quality or truth.
"""
from __future__ import annotations


def model_id(rec: dict) -> str:
    return str(rec.get("id") or rec.get("modelId") or "").strip()


def title(rec: dict) -> str:
    return model_id(rec)


def facets(rec: dict) -> dict:
    mid = model_id(rec)
    org = mid.split("/", 1)[0] if "/" in mid else ""
    f = {
        "source_kind": "model",
        "source_country": "global",
        "entity_type": "model",
        "pipeline_tag": str(rec.get("pipeline_tag") or "").lower(),
        "library": str(rec.get("library_name") or "").lower(),
        "org": org.lower(),
        "downloads": str(rec.get("downloads") if rec.get("downloads") is not None else ""),
        "likes": str(rec.get("likes") if rec.get("likes") is not None else ""),
    }
    created = str(rec.get("createdAt") or "")
    if len(created) >= 4 and created[:4].isdigit():
        f["year"] = created[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = []
    if rec.get("downloads") is not None:
        meta.append(f"{rec['downloads']:,} downloads (last 30 days)")
    if rec.get("likes") is not None:
        meta.append(f"{rec['likes']:,} likes")
    if rec.get("pipeline_tag"):
        meta.append(f"task: {rec['pipeline_tag']}")
    if rec.get("library_name"):
        meta.append(f"library: {rec['library_name']}")
    if rec.get("lastModified"):
        meta.append(f"last modified: {str(rec['lastModified'])[:10]}")
    if meta:
        parts += ["## Adoption",
                  "; ".join(meta)
                  + ".  (self-reported adoption signal — Hub engagement metrics, not a verified fact)",
                  ""]
    tags = rec.get("tags") or []
    if tags:
        parts += ["## Tags", ", ".join(str(t) for t in tags), ""]
    readme = str(rec.get("readme") or "").strip()
    if readme:
        parts += ["## Model card", readme[:24000], ""]
    return "\n".join(parts).strip() + "\n"
