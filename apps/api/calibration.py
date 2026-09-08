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

from api.maps import parse_fact_tag

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
    _KEY_LABEL = {"role": "role", "function": "function", "skill": "skill", "seniority": "level", "metro": "location", "company": "company"}
    weak_evidence_rows = 0
    for f in feedback or []:
        row = rows_by_id.get(f.get("entity_id") or "") or {}
        name = row.get("name") or f.get("entity_id") or "someone"
        tags = [t for t in (f.get("tags") or [])]
        state = f.get("state") or ""
        who = f.get("reviewer_name") or "owner"
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        # STRUCTURED facts (the card's own values, tapped by the reviewer) — one edit per tap
        facts = [x for x in (parse_fact_tag(t) for t in tags) if x]
        for kind, key, val in facts:
            label = val.replace("_", " ")
            if kind == "prefer":
                if key == "company":
                    continue                                   # a company is a target, not a preference
                if label.lower() not in {p.lower() for p in prefer}:
                    prefer.append(label)
                    edits.append(f"{who}: more like {name}'s {_KEY_LABEL.get(key, key)} ({label})")
            elif key == "evidence":
                weak_evidence_rows += 1
                edits.append(f"{who}: {name}'s evidence is too weak")
            elif key == "company":
                n = _norm(val)
                targets = [c for c in facets.get("company", []) if _norm(c) == n]
                if targets:
                    facets["company"] = [c for c in facets["company"] if _norm(c) != n]
                    edits.append(f"{who}: {label} is off ({name}) — removed from the company filter")
                elif n and n not in [_norm(c) for c in exclude_companies]:
                    exclude_companies.append(label)
                    edits.append(f"{who}: {label} is off ({name}) — excluded")
            else:
                if key == "seniority":
                    seniority_soft = True
                if _norm(val) not in avoid:
                    avoid.append(_norm(val))
                    edits.append(f"{who}: {name}'s {_KEY_LABEL.get(key, key)} ({label}) is off — demote")
        # the bare thumbs: 👍 alone prefers the person's role / function; 👎 alone excludes the person
        if ("more_like_this" in tags or state == "shortlist") and not any(k == "prefer" for k, _, _ in facts):
            vals = _row_vals(row, "role") + _row_vals(row, "function") + _row_vals(row, "skill")[:3]
            new = [v for v in vals if v and v.lower() not in {p.lower() for p in prefer}]
            if new:
                prefer += new
                edits.append(f"{who} wants more like {name}: prefer {', '.join(new[:4])}")
        if "less_like_this" in tags or state == "not relevant":
            if row.get("entity_id") and row["entity_id"] not in exclude_ids:
                exclude_ids.append(row["entity_id"])
            legacy_demote = [] if facts else (_row_vals(row, "function") + _row_vals(row, "skill")[:2])
            for v in legacy_demote:
                if _norm(v) not in avoid:
                    avoid.append(_norm(v))
            edits.append(f"{who} wants less like {name}: excluded" + (f"; demote {', '.join(legacy_demote[:3])}" if legacy_demote else ""))
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
    if tag_counts.get("needs_artifact_evidence") or tag_counts.get("evidence_too_weak") or weak_evidence_rows >= 2:
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
    question = re.split(r"\s+[—-]+\s+prefer\s+", brief.strip(), maxsplit=1)[0]   # an earlier revision's tail never stacks
    if prefer:
        question = question + " — prefer " + ", ".join(dict.fromkeys(p.replace("_", " ") for p in prefer[:6]))
    return {"question": question, "refine_facets": facets, "evidence_kinds": ev, "exclude_ids": exclude_ids,
            "exclude_companies": exclude_companies, "avoid_terms": avoid[:12], "edits": edits}


def job_feedback_to_prefs(brief: str, feedback: list[dict], rows_by_ref: dict[str, dict]) -> dict:
    """JOB MAP calibration (tailored to jobs): reviewer taps on a job card's own facts become the
    résumé-match PREFERENCES the next run honors — never a model's judgment of the feedback.
      prefer:title=…     → role_keywords (titles with the word LEAD)      avoid:title=…    → exclude_keywords (dropped)
      prefer:company=…   → prefer_companies (ranked up)                  avoid:company=…  → exclude_companies (dropped)
      prefer:level=…     → seniorities (ranked up)                       avoid:level=…    → avoid_levels (ranked down)
      prefer:location=…  → locations (ranked up)                         avoid:location=… → avoid_locations (ranked down)
      prefer:mode=remote → remote                                        avoid:mode=…     → avoid_modes (ranked down)
      bare 👎            → the posting itself is excluded (seen_ids / exclude_refs)
    Returns {prefs, exclude_refs, edits}."""
    prefs: dict = {"role_keywords": [], "exclude_keywords": [], "prefer_companies": [], "exclude_companies": [],
                   "seniorities": [], "avoid_levels": [], "locations": [], "avoid_locations": [], "avoid_modes": []}
    exclude_refs: list[str] = []
    edits: list[str] = []
    _KEY = {"title": ("role_keywords", "exclude_keywords", "title"), "company": ("prefer_companies", "exclude_companies", "company"),
            "level": ("seniorities", "avoid_levels", "level"), "location": ("locations", "avoid_locations", "location"),
            "mode": ("_mode", "avoid_modes", "work mode")}

    def _add(key, val):
        if val and val not in prefs[key]:
            prefs[key].append(val)
            return True
        return False
    for f in feedback or []:
        ref = str(f.get("entity_id") or "")
        row = rows_by_ref.get(ref) or {}
        label = (row.get("title") or ref or "this role") + ((" @ " + str(row.get("company") or "").replace("_", " ")) if row.get("company") else "")
        who = f.get("reviewer_name") or "owner"
        tags = list(f.get("tags") or [])
        facts = [x for x in (parse_fact_tag(t) for t in tags) if x]
        for kind, key, val in facts:
            if key not in _KEY:
                continue
            pk, ak, human = _KEY[key]
            v = val.replace("_", " ")
            if kind == "prefer":
                if key == "mode":
                    if v == "remote":
                        prefs["remote"] = True; edits.append(f"{who}: more remote roles (from {label})")
                    continue
                if _add(pk, v):
                    edits.append(f"{who}: more like {label}'s {human} ({v})")
            else:
                if _add(ak, v):
                    edits.append(f"{who}: {label}'s {human} ({v}) is off — " + ("excluded" if key in ("title", "company") else "ranked down"))
        if "less_like_this" in tags or f.get("state") == "not relevant":
            if ref and ref not in exclude_refs:
                exclude_refs.append(ref)
                edits.append(f"{who} wants less like {label}: this posting leaves")
        if ("more_like_this" in tags or f.get("state") == "shortlist") and not any(k == "prefer" for k, _, _ in facts):
            # a bare 👍: the posting's title words become role keywords
            words = [w for w in re.split(r"[^a-z0-9+#]+", str(row.get("title") or "").lower()) if len(w) > 3 and w not in _TITLE_STOP][:3]
            new = [w for w in words if w not in prefs["role_keywords"]]
            if new:
                prefs["role_keywords"] += new
                edits.append(f"{who} wants more like {label}: prefer titles with {', '.join(new)}")
    prefs = {k: v for k, v in prefs.items() if v}
    return {"prefs": prefs, "exclude_refs": exclude_refs, "edits": edits}


_TITLE_STOP = {"senior", "junior", "staff", "lead", "principal", "engineer", "manager", "director", "remote", "with", "and", "the", "for"}


def row_ref(r: dict) -> str:
    """The stable identity of a row: a person's entity_id, or a job's id / company|title|location."""
    return str(r.get("entity_id") or r.get("id") or ((r.get("company") or "") + "|" + (r.get("title") or "") + "|" + (r.get("location") or "")))


def diff_rows(before: list[dict], after: list[dict]) -> dict:
    """CODE-COMPUTED delta between two row snapshots (people OR jobs): added, removed, moved (rank
    change ≥ 10), and evidence-type changes for rows present in both."""
    b_ids = [row_ref(r) for r in before]
    a_ids = [row_ref(r) for r in after]
    b_pos = {e: i for i, e in enumerate(b_ids)}
    a_pos = {e: i for i, e in enumerate(a_ids)}
    name = {row_ref(r): (r.get("name") or ((r.get("title") or "") + ((" @ " + str(r.get("company") or "").replace("_", " ")) if r.get("company") else ""))) for r in before + after}
    added = [e for e in a_ids if e not in b_pos]
    removed = [e for e in b_ids if e not in a_pos]
    moved = [(e, b_pos[e], a_pos[e]) for e in a_ids if e in b_pos and abs(a_pos[e] - b_pos[e]) >= 10]
    ev_before = {row_ref(r): ((r.get("evidence") or {}).get("strength") or "") for r in before}
    ev_changed = [(row_ref(r), ev_before.get(row_ref(r)), (r.get("evidence") or {}).get("strength") or "")
                  for r in after if row_ref(r) in ev_before and ((r.get("evidence") or {}).get("strength") or "") != ev_before.get(row_ref(r))]
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
    line: str = ""        # what changed in the LIST (from the counts / names only)


def _list_line(delta: dict) -> str:
    return (f"{delta.get('n_after', 0)} people (was {delta.get('n_before', 0)}): {delta.get('n_added', 0)} new, "
            f"{delta.get('n_removed', 0)} dropped, {delta.get('n_moved', 0)} moved 10+ places.")


async def phrase_delta(llm, delta: dict, edits: list[str]) -> str:
    """ONE plain line about the list, phrased by the LLM FROM THE CODE-COMPUTED DIFF ONLY (temperature 0;
    the prompt carries the counts and the names, nothing else). The brief edits are never rephrased —
    the code-owned edit log is shown verbatim (a rephrase inverted one in testing). Failure → code line."""
    fallback = _list_line(delta)
    if llm is None:
        return fallback
    names = lambda k: ", ".join(str(x.get("name") or "") for x in (delta.get(k) or [])[:5] if x.get("name"))  # noqa: E731
    facts = (f"COUNTS: before={delta.get('n_before', 0)} after={delta.get('n_after', 0)} added={delta.get('n_added', 0)} "
             f"removed={delta.get('n_removed', 0)} moved_10_or_more={delta.get('n_moved', 0)}\n"
             f"ADDED (first few): {names('added') or 'none'}\nREMOVED (first few): {names('removed') or 'none'}")
    try:
        comp = await llm.complete(
            system=("You write ONE plain sentence (≤ 35 words) saying what changed in a revised results list "
                    "(people or job postings — call them 'people' or 'roles' as the names suggest, never "
                    "'candidates'). Use ONLY the counts and the names given. No adjectives about quality, no "
                    "advice, no reasons, no numbers that are not in the facts, no names not given."),
            messages=[{"role": "user", "content": facts[:2000]}],
            response_format=_WhatChanged, max_tokens=200, temperature=0.0)
        line = (getattr(comp.parsed, "line", "") or "").strip()
        # guard: every number the model wrote must be one the facts carried
        allowed = {str(delta.get(k, 0)) for k in ("n_before", "n_after", "n_added", "n_removed", "n_moved")} | {"10"}
        if not line or any(tok not in allowed for tok in re.findall(r"\d+", line)):
            return fallback
        return line
    except Exception:  # noqa: BLE001 — phrasing is cosmetic; the code line always exists
        return fallback
