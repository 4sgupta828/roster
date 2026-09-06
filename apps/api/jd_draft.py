"""JD DRAFT from the centre of peer postings (docs/specs/guided-intake.md §4). Code owns the pipeline: peers =
the nearest open postings for the rough role, deduped by company; requirement lines come from the per-posting
SUMMARIES the product already keeps; ONE model call groups equivalent lines (meaning); the CENTRE = groups that
recur in ≥ JD_PEER_SHARE of peers; ONE model call drafts from the centre + the hiring manager's own words; code
drops any line that cites neither. Comp and work mode are the peers' facet counts, labeled market signal."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from roster_kernel.facets import Contract


def dedupe_peers(rows: list[dict], n: int) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        co = str(r.get("company") or "").strip().lower()
        if co in seen:
            continue
        seen.add(co); out.append(r)
        if len(out) >= n:
            break
    return out


def peer_lines(peers: list[dict], summaries: dict) -> list[tuple[int, str, str]]:
    """(peer_index, kind, text) for every requirement line the peers' summaries hold."""
    out = []
    for i, p in enumerate(peers):
        s = summaries.get(str(p.get("id"))) or summaries.get(p.get("url") or "") or {}
        for kind, key in (("must", "key_requirements"), ("nice", "nice_to_have"), ("responsibility", "responsibilities")):
            for t in (s.get(key) or []):
                t = str(t).strip()
                if t:
                    out.append((i, kind, t[:200]))
    return out


def centre_of(groups: list[dict], n_peers: int, share: float) -> tuple[list[dict], list[dict]]:
    """(centre, optional): groups covering ≥ share of peers vs the rest; each gains an id and a share."""
    centre, optional = [], []
    for gi, g in enumerate(groups or []):
        peers = sorted({int(x) for x in (g.get("peers") or []) if str(x).lstrip("-").isdigit() and 0 <= int(x) < max(n_peers, 1)})
        if not peers or not str(g.get("label") or "").strip():
            continue
        item = {"id": f"g{gi}", "label": str(g["label"]).strip()[:140], "kind": str(g.get("kind") or "must"), "peers": peers,
                "count": len(peers), "share": round(len(peers) / max(n_peers, 1), 2)}
        (centre if item["share"] >= share else optional).append(item)
    centre.sort(key=lambda x: -x["count"]); optional.sort(key=lambda x: -x["count"])
    return centre, optional


def market_signal(peers: list[dict]) -> dict:
    comp, mode, disp = {}, {}, {}
    for p in peers:
        f = p.get("facets") or {}
        for b in (f.get("comp") or []):
            comp[b] = comp.get(b, 0) + 1
            d = (p.get("display") or {}).get("comp")
            if d:
                disp.setdefault(b, d)
        for m in (f.get("work_mode") or []):
            mode[m] = mode.get(m, 0) + 1
    return {"peers": len(peers), "comp": comp, "comp_display": disp, "work_mode": mode,
            "stated_pay": sum(comp.values())}


def _cited(lines: list, centre: list[dict], optional: list[dict]) -> list[dict]:
    """Keep only lines that cite a known group or 'you'; attach the support the UI shows."""
    by_id = {g["id"]: g for g in centre + optional}
    out = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        text = str(ln.get("text") or "").strip()
        src = str(ln.get("source") or "").strip().lower()
        if not text:
            continue
        if src == "you":
            out.append({"text": text, "support": {"you": True}})
        elif src in by_id:
            g = by_id[src]
            out.append({"text": text, "support": {"group": g["id"], "count": g["count"], "peers": g["peers"], "share": g["share"]}})
        # anything else is uncited → dropped (never invented)
    return out


def render_text(draft: dict) -> str:
    """The saved JD text: sections with lines; support is kept in `lines`, not in the prose."""
    parts = [draft.get("title") or "Role", ""]
    if draft.get("summary"):
        parts += [draft["summary"], ""]
    for sec, head in (("responsibilities", "Responsibilities"), ("must_have", "Must have"), ("nice_to_have", "Nice to have")):
        items = draft.get(sec) or []
        if items:
            parts.append(head)
            parts += [f"- {x['text']}" for x in items]
            parts.append("")
    ms = draft.get("market") or {}
    if ms.get("stated_pay"):
        bands = ", ".join(f"{(ms.get('comp_display') or {}).get(b) or b.replace('_', ' ')} ({n})" for b, n in sorted(ms["comp"].items(), key=lambda kv: -kv[1]))
        parts.append(f"Market signal: {ms['stated_pay']} of {ms['peers']} peer postings state pay — {bands}.")
    if ms.get("work_mode"):
        parts.append("Peers' work mode: " + ", ".join(f"{m} ({n})" for m, n in sorted(ms["work_mode"].items(), key=lambda kv: -kv[1])) + ".")
    return "\n".join(parts).strip() + "\n"


async def build_jd_draft(*, role_text: str, context: dict, evaluate_fn: Callable[[dict], Awaitable[dict]],
                         summaries_fn: Callable[[list[dict]], Awaitable[dict]], compile_fn: Callable[..., Contract],
                         llm_json: Callable[[str, str], dict], n_peers: int = 10, pool: int = 40, share: float = 0.30,
                         prefer: dict | None = None, min_match: int = 35) -> dict:
    from roster_vertical.intake import draft_prompt, group_prompt
    # 1) peers
    try:
        c = compile_fn("job", role_text, limit=pool, scope={})
    except Exception:   # noqa: BLE001
        c = Contract(kind="job", text=role_text[:500], limit=pool)
    c.limit = pool
    named_role = bool(c.must.get("field") or c.prefer.get("field") or c.prefer.get("role_family") or c.must.get("role_family") or c.prefer.get("specialty"))
    if not named_role:
        # "I'm hiring. Keep going" names no role: no neighbourhood to read (it once drafted from recruiter postings)
        draft = {"title": str(context.get("title") or role_text[:80]).strip(), "summary": "", "responsibilities": [], "must_have": [], "nice_to_have": [],
                 "peers": [], "centre": [], "optional": [], "market": market_signal([]), "sparse": True, "no_role": True,
                 "sources": {"posting_ids": [], "role_text": role_text[:500], "context": dict(context or {})}}
        draft["text"] = render_text(draft)
        return draft
    # peers are the role's similarity NEIGHBOURHOOD: only the field filters; every other compiled must only ranks
    for k in [k for k in list(c.must.keys()) if k != "field"]:
        vals = c.must.pop(k)
        if isinstance(vals, list):
            c.prefer[k] = sorted(set(list(c.prefer.get(k) or []) + [str(v) for v in vals]))
    if prefer:
        for k, v in prefer.items():
            c.prefer.setdefault(k, list(v))
    out = await evaluate_fn(c.to_dict())
    # only postings that actually resemble the role count as peers (calibrated match above the noise floor);
    # "Keep going" as a role text once produced ten ICU-nurse peers
    relevant = [r for r in (out.get("rows") or []) if r.get("match_pct") is None or int(r.get("match_pct") or 0) >= min_match]
    peers = dedupe_peers(relevant, n_peers)
    # 2) requirement lines from the peers' summaries
    summaries = await summaries_fn(peers) if peers else {}
    lines = peer_lines(peers, summaries)
    centre: list[dict] = []; optional: list[dict] = []
    sparse = len(peers) < 5 or len(lines) < 5
    if not sparse:
        tagged = "\n".join(f"[p{i}] {kind}: {text}" for i, kind, text in lines)
        try:
            grouped = await asyncio.to_thread(llm_json, group_prompt(), tagged[:12000])
            centre, optional = centre_of(list(grouped.get("groups") or []), len(peers), share)
        except Exception:   # noqa: BLE001
            centre, optional = [], []
    # 3) the draft
    ctx_lines = [f"{k.replace('_', ' ')}: {v}" for k, v in (context or {}).items() if v]
    user = ("MANAGER'S ROLE — the title, level and team come from HERE (cite as \"you\"): " + role_text[:600] + "\n"
            + "MANAGER'S CONTEXT AND MUST-HAVES (cite as \"you\"):\n" + ("\n".join(ctx_lines) if ctx_lines else "(none beyond the role text)") + "\n\n"
            + "CENTRE (recurs across peers):\n" + ("\n".join(f"{g['id']} [{g['kind']}] {g['label']} — {g['count']} of {len(peers)} peers" for g in centre) or "(none — draft from the manager's words only)")
            + "\n\nSOME PEERS ALSO ASK:\n" + ("\n".join(f"{g['id']} [{g['kind']}] {g['label']} — {g['count']} of {len(peers)}" for g in optional[:12]) or "(none)"))
    try:
        d = await asyncio.to_thread(llm_json, draft_prompt(), user)
    except Exception:   # noqa: BLE001
        d = {}
    draft = {"title": str(d.get("title") or context.get("title") or role_text[:80]).strip(), "summary": str(d.get("summary") or "").strip()[:600],
             "responsibilities": _cited(d.get("responsibilities") or [], centre, optional), "must_have": _cited(d.get("must_have") or [], centre, optional),
             "nice_to_have": _cited(d.get("nice_to_have") or [], centre, optional),
             "peers": [{"id": p.get("id"), "company": p.get("company"), "title": p.get("title"), "url": p.get("url")} for p in peers],
             "centre": centre, "optional": optional[:12], "market": market_signal(peers), "sparse": sparse,
             "sources": {"posting_ids": [p.get("id") for p in peers], "role_text": role_text[:500], "context": dict(context or {})}}
    draft["text"] = render_text(draft)
    return draft


async def redraft(draft: dict, change: str, llm_json: Callable[[str, str], dict]) -> dict:
    """Apply a change request in words; the request counts as the manager's words ('you'); the citation rule holds."""
    from roster_vertical.intake import draft_prompt
    centre, optional = list(draft.get("centre") or []), list(draft.get("optional") or [])
    cur = {k: [{"text": x["text"], "source": ("you" if (x.get("support") or {}).get("you") else (x.get("support") or {}).get("group"))} for x in (draft.get(k) or [])]
           for k in ("responsibilities", "must_have", "nice_to_have")}
    user = ("CURRENT DRAFT (JSON): " + __import__("json").dumps({"title": draft.get("title"), "summary": draft.get("summary"), **cur})
            + "\n\nMANAGER'S CHANGE REQUEST (cite new lines as \"you\"): " + change
            + "\n\nGROUPS you may still cite:\n" + "\n".join(f"{g['id']} [{g['kind']}] {g['label']} — {g['count']} peers" for g in centre + optional))
    try:
        d = await asyncio.to_thread(llm_json, draft_prompt(), user)
    except Exception:   # noqa: BLE001
        return draft
    new = dict(draft)
    new.update({"title": str(d.get("title") or draft.get("title") or "").strip(), "summary": str(d.get("summary") or draft.get("summary") or "").strip()[:600],
                "responsibilities": _cited(d.get("responsibilities") or [], centre, optional), "must_have": _cited(d.get("must_have") or [], centre, optional),
                "nice_to_have": _cited(d.get("nice_to_have") or [], centre, optional)})
    new["sources"] = {**(draft.get("sources") or {}), "changes": list((draft.get("sources") or {}).get("changes") or []) + [change[:300]]}
    new["text"] = render_text(new)
    return new
