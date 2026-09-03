"""HIRING-MANAGER CALIBRATION (recruiter-workflows P2b) — feedback → a revised search contract.

Reviewers mark rows with tags (`maps.FEEDBACK_TAGS`). CODE turns those tags into edits of the NEXT
map's inputs — never of the evidence, never through a model's judgment of what the feedback
"means". Every edit is recorded in plain words so the owner sees why the brief changed. The LLM's
only role afterwards is to phrase a two-line summary from the code-computed diff.

  more_like_this          → the rows' role / function / skill values become PREFERENCES (question augmentation)
  less_like_this          → the rows are EXCLUDED and their distinctive values DEMOTED
  wrong_domain            → the rows' function / skill values are demoted
  wrong_seniority         → seniority stops gating; the rows' level is demoted
  wrong_location          → the rows' metro is demoted (the scope itself is the user's selector)
  wrong_company_target    → the rows' company leaves the target list (if named) or joins the exclusions
  evidence_too_weak /
  needs_artifact_evidence → require linked public work (any kind)
  self_stated_is_enough /
  private_company_talent  → drop the evidence requirement
"""
from __future__ import annotations

import re

from pydantic import BaseModel

_EVIDENCE_ALL = ["paper", "repo", "post", "talk", "patent"]
_DEMOTE_KEYS = ("role", "function", "skill", "seniority", "metro")


def _norm(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(v or "").lower()).strip("_")


def _row_vals(row: dict, key: str) -> list[str]:
    return [str(a.get("display") or "").replace("_", " ") for a in (row.get("attributes") or []) if a.get("key") == key and a.get("display")]


def feedback_to_contract(brief: str, filters: dict, evidence_kinds: list[str], feedback: list[dict],
                         rows_by_id: dict[str, dict]) -> dict:
    """Return the revised inputs + the plain-words edit log:
    {question, refine_facets, evidence_kinds, exclude_ids, exclude_companies, avoid_terms, edits}"""
    facets = {k: list(v) for k, v in (filters or {}).items() if isinstance(v, list)}
    ev = list(evidence_kinds or [])
    edits: list[str] = []
    prefer: list[str] = []
    exclude_ids: list[str] = []
    exclude_companies: list[str] = []
    avoid: list[str] = []
    seniority_soft = False
    tag_counts: dict[str, int] = {}
    for f in feedback or []:
        row = rows_by_id.get(f.get("entity_id") or "") or {}
        name = row.get("name") or f.get("entity_id") or "someone"
        tags = [t for t in (f.get("tags") or [])]
        state = f.get("state") or ""
        who = f.get("reviewer_name") or "owner"
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        if "more_like_this" in tags or state == "shortlist":
            vals = _row_vals(row, "role") + _row_vals(row, "function") + _row_vals(row, "skill")[:3]
            new = [v for v in vals if v and v.lower() not in {p.lower() for p in prefer}]
            if new:
                prefer += new
                edits.append(f"{who} wants more like {name}: prefer {', '.join(new[:4])}")
        if "less_like_this" in tags or state == "not relevant":
            if row.get("entity_id") and row["entity_id"] not in exclude_ids:
                exclude_ids.append(row["entity_id"])
            vals = _row_vals(row, "function") + _row_vals(row, "skill")[:2]
            for v in vals:
                if _norm(v) not in avoid:
                    avoid.append(_norm(v))
            edits.append(f"{who} wants less like {name}: excluded" + (f"; demote {', '.join(vals[:3])}" if vals else ""))
        if "wrong_domain" in tags:
            vals = _row_vals(row, "function") + _row_vals(row, "skill")[:2]
            for v in vals:
                if _norm(v) not in avoid:
                    avoid.append(_norm(v))
            if vals:
                edits.append(f"{who}: {name} is the wrong domain — demote {', '.join(vals[:3])}")
        if "wrong_seniority" in tags:
            seniority_soft = True
            for v in _row_vals(row, "seniority"):
                if _norm(v) not in avoid:
                    avoid.append(_norm(v))
                    edits.append(f"{who}: {name} is the wrong level — demote '{v}'")
        if "wrong_location" in tags:
            for v in _row_vals(row, "metro"):
                if _norm(v) not in avoid:
                    avoid.append(_norm(v))
                    edits.append(f"{who}: {name} is in the wrong place — demote {v}")
        if "wrong_company_target" in tags:
            for co in _row_vals(row, "company"):
                n = _norm(co)
                targets = [c for c in facets.get("company", []) if _norm(c) == n]
                if targets:
                    facets["company"] = [c for c in facets["company"] if _norm(c) != n]
                    edits.append(f"{who}: {co} is the wrong target — removed from the company filter")
                elif n and n not in [_norm(c) for c in exclude_companies]:
                    exclude_companies.append(co)
                    edits.append(f"{who}: {co} is the wrong target — excluded")
    if tag_counts.get("needs_artifact_evidence") or tag_counts.get("evidence_too_weak"):
        if not ev:
            ev = list(_EVIDENCE_ALL)
            edits.append("reviewers want linked public work — the next map requires at least one linked paper, repo, post, talk or patent")
    if tag_counts.get("self_stated_is_enough") or tag_counts.get("private_company_talent"):
        if ev:
            ev = []
            edits.append("reviewers accept self-stated profiles / private-company talent — the evidence requirement is dropped")
    if seniority_soft and facets.get("seniority"):
        facets.pop("seniority", None)
        edits.append("seniority no longer gates the map (reviewers flagged wrong levels) — it ranks instead")
    if not facets.get("company"):
        facets.pop("company", None)
    question = brief.strip()
    if prefer:
        question = question + " — prefer " + ", ".join(dict.fromkeys(p.replace("_", " ") for p in prefer[:6]))
    return {"question": question, "refine_facets": facets, "evidence_kinds": ev, "exclude_ids": exclude_ids,
            "exclude_companies": exclude_companies, "avoid_terms": avoid[:12], "edits": edits}


def diff_rows(before: list[dict], after: list[dict]) -> dict:
    """CODE-COMPUTED delta between two row snapshots: added, removed, moved (rank change ≥ 10),
    and evidence-type changes for people present in both."""
    b_ids = [r.get("entity_id") for r in before]
    a_ids = [r.get("entity_id") for r in after]
    b_pos = {e: i for i, e in enumerate(b_ids)}
    a_pos = {e: i for i, e in enumerate(a_ids)}
    name = {r.get("entity_id"): r.get("name") for r in before + after}
    added = [e for e in a_ids if e not in b_pos]
    removed = [e for e in b_ids if e not in a_pos]
    moved = [(e, b_pos[e], a_pos[e]) for e in a_ids if e in b_pos and abs(a_pos[e] - b_pos[e]) >= 10]
    ev_before = {r.get("entity_id"): ((r.get("evidence") or {}).get("strength") or "") for r in before}
    ev_changed = [(r.get("entity_id"), ev_before.get(r.get("entity_id")), (r.get("evidence") or {}).get("strength") or "")
                  for r in after if r.get("entity_id") in ev_before and ((r.get("evidence") or {}).get("strength") or "") != ev_before.get(r.get("entity_id"))]
    return {"added": [{"entity_id": e, "name": name.get(e)} for e in added[:40]], "n_added": len(added),
            "removed": [{"entity_id": e, "name": name.get(e)} for e in removed[:40]], "n_removed": len(removed),
            "moved": [{"entity_id": e, "name": name.get(e), "from": bp + 1, "to": ap + 1} for e, bp, ap in moved[:20]], "n_moved": len(moved),
            "evidence_changed": [{"entity_id": e, "name": name.get(e), "from": a, "to": b} for e, a, b in ev_changed[:20]],
            "n_before": len(before), "n_after": len(after)}


def diff_text(delta: dict, edits: list[str]) -> str:
    """The code-owned fallback narrative (the LLM may rephrase this, never add to it)."""
    parts = [f"{delta.get('n_after', 0)} people (was {delta.get('n_before', 0)}): {delta.get('n_added', 0)} new, "
             f"{delta.get('n_removed', 0)} dropped, {delta.get('n_moved', 0)} moved ≥10 places."]
    if edits:
        parts.append("Brief changes: " + "; ".join(edits[:4]) + ("." if not edits[0].endswith(".") else ""))
    return " ".join(parts)


class _WhatChanged(BaseModel):
    line_1: str = ""      # what changed in the brief (from the edit log only)
    line_2: str = ""      # what changed in the list (from the counts / names only)


async def phrase_delta(llm, delta: dict, edits: list[str]) -> str:
    """Two plain lines phrased by the LLM FROM THE CODE-COMPUTED DIFF ONLY (temperature 0; the prompt
    carries the numbers, the names and the edit log, nothing else). Any failure → `diff_text`."""
    fallback = diff_text(delta, edits)
    if llm is None:
        return fallback
    names = lambda k: ", ".join(str(x.get("name") or "") for x in (delta.get(k) or [])[:5] if x.get("name"))  # noqa: E731
    facts = (f"COUNTS: before={delta.get('n_before', 0)} after={delta.get('n_after', 0)} added={delta.get('n_added', 0)} "
             f"removed={delta.get('n_removed', 0)} moved_10_or_more={delta.get('n_moved', 0)}\n"
             f"ADDED (first few): {names('added') or 'none'}\nREMOVED (first few): {names('removed') or 'none'}\n"
             f"BRIEF EDITS (code-owned, in order):\n" + ("\n".join(f"- {e}" for e in edits[:8]) or "- none"))
    try:
        comp = await llm.complete(
            system=("You write a two-line 'what changed' note for a recruiter's revised candidate list. Use ONLY "
                    "the facts given: line_1 restates the brief edits in plain words (≤ 30 words); line_2 states "
                    "what changed in the list using the counts and the named people (≤ 30 words). No adjectives "
                    "about quality, no advice, no numbers that are not in the facts, no people not named."),
            messages=[{"role": "user", "content": facts[:3000]}],
            response_format=_WhatChanged, max_tokens=300, temperature=0.0)
        l1 = (getattr(comp.parsed, "line_1", "") or "").strip()
        l2 = (getattr(comp.parsed, "line_2", "") or "").strip()
        # guard: every number the model wrote must be one the facts carried
        allowed = {str(delta.get(k, 0)) for k in ("n_before", "n_after", "n_added", "n_removed", "n_moved")} | {"10"}
        for tok in re.findall(r"\d+", l1 + " " + l2):
            if tok not in allowed:
                return fallback
        return "\n".join(x for x in (l1, l2) if x) or fallback
    except Exception:  # noqa: BLE001 — phrasing is cosmetic; the code narrative always exists
        return fallback
