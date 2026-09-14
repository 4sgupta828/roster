"""Ephemeral live-people leg for Find-candidates (app glue).

Stage 1: fetch candidates from the live providers (PDL/Exa, via the vertical), score them in-memory
against the SAME query vector as the corpus leg, apply a source-calibration down-weight so short
self-stated snippets don't systematically out-rank rich corpus bios, dedupe CONSERVATIVELY against
the corpus rows (cluster on name+company; never overwrite), and hand back people-cards for blending.

NOTHING is written to the corpus here — live rows carry citation=None and a `source`/`found_by`
label so they are never mistaken for grounded evidence. Durable ingestion of a shortlisted person
is a separate, async Stage-2 step. Behind ROSTER_LIVE_PEOPLE (default OFF → byte-identical path).
"""
from __future__ import annotations

import math
import os
import re

from roster_kernel.providers.base import ProviderMode, resolve_mode

from api.people_population import calibrated_pct, embed_texts

_log = __import__("logging").getLogger("roster.live_people")


def live_people_enabled() -> bool:
    """Flag (default OFF, Rule 20): blend live PDL/Exa candidates into Find-candidates. OFF → the
    corpus-only path runs unchanged."""
    return os.environ.get("ROSTER_LIVE_PEOPLE", "").lower() in ("1", "true", "yes")


def _weight() -> float:
    """Source-calibration factor applied to a live row's cosine (panel guidance: sparse self-stated
    text over-projects onto a keyword-y query). Tunable; default 0.85."""
    try:
        return max(0.1, min(1.0, float(os.environ.get("ROSTER_LIVE_PEOPLE_WEIGHT", "0.85"))))
    except ValueError:
        return 0.85


def _cap() -> int:
    try:
        return max(1, int(os.environ.get("ROSTER_LIVE_PEOPLE_CAP", "12")))
    except ValueError:
        return 12


def _parse_vec(s: str | None) -> list[float]:
    if not s:
        return []
    try:
        return [float(x) for x in str(s).strip().lstrip("[").rstrip("]").split(",") if x.strip()]
    except ValueError:
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _norm_co(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _existing_keys(existing_cards: list[dict]) -> set[tuple[str, str]]:
    """(_norm_name, _norm_co) pairs already present in the corpus result — for CONSERVATIVE dedupe
    (drop a live row only when BOTH name and a known company match; ambiguity keeps the row, since a
    false-merge is worse than a duplicate)."""
    keys: set[tuple[str, str]] = set()
    for c in existing_cards or []:
        nm = _norm_name(c.get("name") or "")
        cos = [a.get("display") for a in (c.get("attributes") or []) if a.get("key") == "company"]
        if nm:
            for co in (cos or [""]):
                keys.add((nm, _norm_co(co or "")))
    return keys


def build_live_search(*, cassette_root=None, clients=None, sources=None):
    """Build the live people search. There are no people cassettes yet, so the leg calls its
    providers DIRECTLY whenever a key exists — independent of ROSTER_PROVIDER_MODE (which governs
    LLM/web replay/record, and would otherwise wrongly force this leg into replay or a bad-root
    record). A cassette_root (eval/CI) opts back into the recorded path; tests inject `clients`."""
    from roster_vertical.live_people import build_live_people_search, live_people_available
    if clients is not None or cassette_root:
        return build_live_people_search(mode=resolve_mode(), cassette_root=(cassette_root or ""),
                                        clients=clients)
    if not live_people_available():
        _log.warning("live people leg on but no PDL_API_KEY / EXA_API_KEY configured")
        return None
    from roster_kernel.providers.record_search import CompositeRecordSearch
    from roster_vertical.live_people import ExaPeopleSearch, PdlPeopleSearch
    picked = [s for s in (sources or ("exa", "pdl")) if s in ("exa", "pdl")] or ["exa", "pdl"]
    built = {"exa": ExaPeopleSearch, "pdl": PdlPeopleSearch}
    return CompositeRecordSearch([built[s]() for s in picked])


async def merge_live_candidates(qvec: str, prefs: dict, existing_cards: list[dict], *,
                                search_client=None, sources=None, max_out: int | None = None) -> list[dict]:
    """Return scored live people-cards (each with a transient `_score`), ranked by relevance. `sources`
    restricts to specific providers (['exa'] / ['pdl'] / both); `max_out` caps how many are returned
    (defaults to the small blend cap). Fail-safe: any error → []. No DB writes."""
    prefs = prefs or {}
    want_src = {str(s).lower() for s in (sources or []) if str(s).lower() in ("exa", "pdl")}
    client = search_client if search_client is not None else build_live_search(sources=sorted(want_src) or None)
    if client is None:
        return []
    cap = int(max_out) if max_out else _cap()
    from roster_vertical.live_people import normalize_record

    filters = {"skills": [str(s) for s in (prefs.get("skills") or [])][:8],
               "locations": [str(l) for l in (prefs.get("locations") or [])][:6],
               "country": (prefs.get("country") or "").lower(),
               "search_text": str(prefs.get("search_text") or "")[:2000],
               "seniorities": [str(s).lower() for s in (prefs.get("seniorities") or [])]}
    query = (filters["search_text"] or " ".join(filters["skills"]) or "").strip() or "candidates"
    try:
        records = await client.search(query, max_results=max(cap * 2, 20), filters=filters)
    except Exception as ex:   # noqa: BLE001 — additive leg
        _log.warning("live people search failed: %s", ex)
        return []
    _log.info("live people: %d raw records for %r", len(records or []), query[:60])

    cards = [c for c in (normalize_record(r) for r in (records or [])) if c]
    if want_src:                                          # restrict to the picked providers
        cards = [c for c in cards if c.get("source") in want_src]
    if not cards:
        return []

    seen = _existing_keys(existing_cards)
    want_sens = {str(s).lower() for s in (prefs.get("seniorities") or []) if str(s).strip()}
    want_country = (prefs.get("country") or "").lower()
    want_skills = [str(k).lower().strip() for k in (prefs.get("skills") or []) if str(k).strip()][:8]
    excl = {_norm_co(c) for c in (prefs.get("exclude_companies") or []) if _norm_co(str(c))}

    kept: list[dict] = []
    for c in cards:
        lf = c.get("_live_fields") or {}
        nm, co = _norm_name(c.get("name") or ""), _norm_co(lf.get("company") or "")
        # CONSERVATIVE dedupe: drop only on an exact name+known-company collision with a corpus row.
        if nm and co and (nm, co) in seen:
            continue
        # hard filters mirrored from the corpus leg
        country = (lf.get("country") or "").lower()
        if want_country and country and country != want_country:
            continue
        if excl and co and co in excl:
            continue
        kept.append(c)
    if not kept:
        return []

    q = _parse_vec(qvec)
    vecs = embed_texts([(c.get("_live_fields") or {}).get("text") or c.get("name") or "" for c in kept])
    out: list[dict] = []
    w = _weight()
    for c, v in zip(kept, vecs):
        cos = _cosine(q, _parse_vec(v)) if v else 0.0
        sim = max(0.0, cos) * w                        # source calibration
        lf = c.get("_live_fields") or {}
        score, reasons = sim, []
        sen_hit = next((s for s in want_sens if s and s in (lf.get("title") or "").lower()), "")
        if want_sens and sen_hit:
            score += 0.12; reasons.append(f"{sen_hit.replace('_',' ')} level")
        if want_skills:
            hay = (" ".join(lf.get("skills") or []) + " " + (lf.get("title") or "")).lower().replace("_", " ")
            hits = [k for k in want_skills if k.replace("_", " ") in hay]
            if hits:
                score += min(0.15, 0.05 * len(hits)); reasons.append("skills: " + ", ".join(hits[:3]))
        reasons.append(f"live · {c.get('source') or 'external'}")
        c["match_pct"] = calibrated_pct(sim)
        c["reasons"] = reasons
        c["live"] = True                               # UI badge: unverified live source
        c["_score"] = score
        c.pop("_live_fields", None)
        out.append(c)
    out.sort(key=lambda x: -x["_score"])
    return out[:cap]
