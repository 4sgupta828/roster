"""EVIDENCE TYPING for the talent-intelligence redesign (docs/specs/talent-intelligence-redesign.md).

The spec's core discipline: evidence is TYPED, not flattened. Every person row carries an evidence
packet saying what KIND of support each claim has — self-stated, employer-stated, artifact-backed,
structured, corroborated — plus gaps. Classification is CODE-OWNED, derived from each facet's
source provenance (never model-asserted), so a self-stated bio can never silently render as
verified. App-level module (recruiting vocabulary allowed); the kernel stays domain-free.

Source-family → evidence type, grounded in what each source actually IS:
- github      → self_stated      (facets are extracted from the person's own profile/bio)
- openalex    → structured       (scholarly registry; affiliations/concepts derive from
                                  published works — noted as publication-derived)
- yc/npi/sec  → structured       (constrained public registries)
- theorg      → employer_stated  (organization chart pages)
- aifund/ef/pear/sosv → employer_stated (portfolio/program pages)
CORROBORATED = the same claim value supported by ≥2 INDEPENDENT families (rare today: entities are
not yet cross-source deduped — honesty over inflation).
"""
from __future__ import annotations

# family detection from a facet's source_document_id (fallback: the entity id prefix)
_DOC_FAMILY_PATTERNS = (
    ("openalex.org", "openalex"), ("github", "github"), ("ycombinator.com", "yc"),
    ("npiregistry", "npi"), ("theorg.com", "theorg"), ("edgar", "sec"), ("sec.gov", "sec"),
    ("aifund.ai", "aifund"), ("joinef.com", "ef"), ("pear.vc", "pear"), ("sosv", "sosv"),
    ("linkedin.com", "linkedin"), ("youtube.com", "youtube"), ("youtu.be", "youtube"), ("vimeo.com", "vimeo"),
)

FAMILY_EVIDENCE_TYPE = {
    "github": "self_stated",
    "openalex": "structured",
    "yc": "structured",
    "npi": "structured",
    "sec": "structured",
    "theorg": "employer_stated",
    "aifund": "employer_stated",
    "ef": "employer_stated",
    "pear": "employer_stated",
    "sosv": "employer_stated",
    "linkedin": "self_stated",      # headline quoted from a search-engine snippet of the profile
    "site": "self_stated",          # the person's own site / newsletter (self-published writing)
    "youtube": "artifact_backed", "vimeo": "artifact_backed",
}

# ladder position (higher = stronger support for what the source constrains)
_STRENGTH_RANK = {"corroborated": 4, "structured": 3, "artifact_backed": 3,
                  "employer_stated": 2, "self_stated": 1}

EVIDENCE_TYPE_LABELS = {
    "self_stated": "self-stated",
    "employer_stated": "employer-stated",
    "artifact_backed": "artifact-backed",
    "structured": "structured record",
    "corroborated": "corroborated",
}

# human framing per family for the Evidence Rail ("where this comes from")
FAMILY_LABELS = {
    "github": "the person's own GitHub profile",
    "openalex": "OpenAlex scholarly registry (derived from published works)",
    "yc": "Y Combinator company directory",
    "npi": "NPI registry (US licensed-provider directory)",
    "sec": "SEC EDGAR filings",
    "theorg": "TheOrg company org-chart page",
    "aifund": "AI Fund portfolio page",
    "ef": "Entrepreneur First company page",
    "pear": "Pear VC portfolio page",
    "sosv": "SOSV portfolio page",
    "linkedin": "the person's LinkedIn headline, quoted from a search-engine snippet (self-stated)",
    "site": "the person's own site or newsletter (declared on their profile)",
    "youtube": "a recorded talk (matched on name + employer — verify)",
    "vimeo": "a recorded talk (matched on name + employer — verify)",
}


# facet key → CLAIM AXIS: enrichment facets that restate an existing claim under their own key (the
# facet table's key is (entity, key, value) without the source) count toward that claim's
# agreement across families. Display stays per facet key; agreement is per axis.
CLAIM_AXIS = {"linkedin_company": "company"}


def facet_family(source_document_id: str, entity_id: str = "") -> str:
    """The source FAMILY a facet came from — per-facet provenance first, entity prefix fallback."""
    doc = (source_document_id or "").lower()
    for pat, fam in _DOC_FAMILY_PATTERNS:
        if pat in doc:
            return fam
    return (entity_id or "").split(":", 1)[0] if ":" in (entity_id or "") else ""


def evidence_packet(facet_rows: list[dict], entity_id: str = "") -> dict:
    """CODE-OWNED evidence packet for one person row.

    Returns {types, strength, families, per_key, corroborated_keys, gaps}:
    - types: evidence types present, strongest first;
    - strength: the row's headline — the strongest type present, or 'mixed' when families of
      different rungs coexist;
    - per_key: facet_key → {type, family} for the Evidence Rail;
    - corroborated_keys: claim keys where ≥2 independent families agree on a value;
    - gaps: useful dimensions with no evidence at all (honest missing-field accounting).
    """
    fams_by_keyval: dict[tuple, set] = {}
    per_key: dict[str, dict] = {}
    families: set[str] = set()
    for f in facet_rows or []:
        key = f.get("facet_key") or ""
        fam = facet_family(f.get("document_id") or f.get("source_document_id") or "", entity_id)
        etype = FAMILY_EVIDENCE_TYPE.get(fam, "self_stated")
        if fam:
            families.add(fam)
        val = (f.get("value_norm") or f.get("facet_value_norm")
               or (f.get("display_value") or "").lower())
        fams_by_keyval.setdefault((CLAIM_AXIS.get(key, key), val), set()).add(fam)
        cur = per_key.get(key)
        if cur is None or _STRENGTH_RANK.get(etype, 0) > _STRENGTH_RANK.get(cur["type"], 0):
            per_key[key] = {"type": etype, "family": fam}
    # CORROBORATED = the same value from ≥2 independent families of which at least one is NOT
    # self-authored (a registry, an employer page, an artifact). Two self-stated sources agreeing
    # (GitHub bio + LinkedIn headline) is CONSISTENCY — real signal for calibration, not verification.
    corroborated_keys, consistent_keys = [], []
    for (k, v), fams in fams_by_keyval.items():
        fams = {f for f in fams if f}
        if len(fams) < 2 or not v:
            continue
        if all(FAMILY_EVIDENCE_TYPE.get(f, "self_stated") == "self_stated" for f in fams):
            consistent_keys.append(k)
        else:
            corroborated_keys.append(k)
    corroborated_keys, consistent_keys = sorted(set(corroborated_keys)), sorted(set(consistent_keys))
    for k in corroborated_keys:
        per_key[k] = {**per_key.get(k, {}), "type": "corroborated"}
    types_present = sorted({d["type"] for d in per_key.values()},
                           key=lambda t: (-_STRENGTH_RANK.get(t, 0), t))   # rank, then a STABLE tie-break
    if not types_present:
        strength = "gap"
    elif len({_STRENGTH_RANK.get(t, 0) for t in types_present}) > 1:
        strength = "mixed"
    else:
        strength = types_present[0]
    _USEFUL = ("role", "seniority", "function", "company", "metro", "country")
    gaps = [k for k in _USEFUL if k not in per_key]
    return {"types": types_present, "strength": strength,
            "families": sorted(f for f in families if f), "per_key": per_key,
            "corroborated_keys": corroborated_keys, "consistent_keys": consistent_keys, "gaps": gaps}


def _co_norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def calibrate(packet: dict, row: dict) -> dict:
    """SELF-STATED CALIBRATION (evidence-model-v2 §3, first band): a self-stated claim becomes a
    claim with a prior. CODE-OWNED reasons from independent, grounded signals we hold on the row:
      - consistency: the same value stated in ≥2 self-authored sources (GitHub bio + LinkedIn headline);
      - artifact agreement: a paper affiliation or a public GitHub org that names the stated company.
    Band ∈ consistent | uncorroborated | n/a (no self-stated claim). Reasons are shown, never a score;
    'contradicted' waits for dated evidence (temporal consistency) — not asserted from undated facets."""
    if not isinstance(packet, dict):
        return packet
    reasons: list[str] = []
    for k in packet.get("consistent_keys") or []:
        reasons.append(f"{k.replace('_', ' ')} stated consistently across independent self-authored profiles")
    attrs = row.get("attributes") or []
    companies = [_co_norm(a.get("display")) for a in attrs if a.get("key") == "company" and a.get("display")]
    art = row.get("artifacts") or {}
    for co in companies:
        if len(co) < 3:
            continue
        n_aff = sum(int(a.get("n") or 0) for a in art.get("affiliations") or []
                    if co in _co_norm(a.get("name")) or _co_norm(a.get("name")) in co)
        if n_aff:
            reasons.append(f"{n_aff} published work{'s' if n_aff != 1 else ''} carry an affiliation matching the stated company")
        orgs = [it for it in art.get("items") or [] if it.get("kind") == "org"
                and (co.replace(" ", "") in _co_norm(it.get("title")).replace(" ", ""))]
        if orgs:
            reasons.append("public GitHub org membership matches the stated company")
    if "linkedin" in (packet.get("families") or []) and any(k == "company" for k in packet.get("consistent_keys") or []):
        reasons.append("note: the LinkedIn profile was matched on name + company, so its agreement is consistency, not independent proof")
    has_self = "self_stated" in (packet.get("types") or []) or any(
        d.get("type") == "self_stated" for d in (packet.get("per_key") or {}).values())
    band = "n/a" if not has_self else ("consistent" if reasons else "uncorroborated")
    packet["calibration"] = {"band": band, "reasons": reasons}
    return packet


def evidence_groups(rows: list[dict]) -> list[dict]:
    """Map-level distribution for the coverage panel: how many rows per headline evidence state —
    the spec's candidate groups, counted by code from the packets."""
    order = ("corroborated", "structured", "artifact_backed", "employer_stated",
             "self_stated", "mixed", "gap")
    counts: dict[str, int] = {}
    for r in rows or []:
        s = ((r.get("evidence") or {}).get("strength")) or "gap"
        counts[s] = counts.get(s, 0) + 1
    return [{"state": s, "label": EVIDENCE_TYPE_LABELS.get(s, s), "count": counts[s]}
            for s in order if counts.get(s)]


BAND = 0.05          # relevance band width: evidence reorders ONLY inside a band
_SENIOR_WORDS = {"senior", "staff", "principal", "lead", "head", "director", "vp", "c_level", "cto",
                 "distinguished_scientist", "founder", "senior_manager", "engineering_manager"}


def _facet_terms(brief_facets: dict | None) -> list[str]:
    """Tokens from the brief's compiled skill/function/role facets ('vector_db' → 'vector db')."""
    out: list[str] = []
    for k in ("skill", "function", "role"):
        for v in (brief_facets or {}).get(k) or []:
            t = str(v).strip().lower().replace("_", " ")
            if len(t) >= 3:
                out.append(t)
    return out


def _stat_number(stat: str) -> int:
    import re
    m = re.match(r"\s*([\d,]+)", stat or "")
    return int(m.group(1).replace(",", "")) if m else 0


def rank_read(row: dict, brief_facets: dict | None = None) -> dict:
    """CODE-OWNED rank read for one row (evidence-model-v2 §4).

    RELEVANCE BAND is the primary key: similarity (or the facet-path score) bucketed into 0.05 bands,
    so evidence reorders near-equals and a weak match can never outrank a strong one because it has
    many repos. WITHIN a band the evidence score orders rows:
      - corroborated / consistent affiliation; artifact-backed capability;
      - footprint (log-scaled, capped — prolific accounts do not dominate); freshness (≤2 years);
      - BRIEF-AWARE capability: artifacts whose title/venue/language match the brief's skill,
        function or role terms count more than generic footprint;
      - SENIORITY FROM EVIDENCE when the brief asks for senior people: first-author papers,
        citations, starred repos, org membership — not the self-stated seniority word alone;
      - LinkedIn headline ↔ brief fit when the snippet was read;
      - a small penalty for 'scanned and nothing found'; unscanned is neutral (unknown ≠ absent).
    Returns {score, band, relevance, within, headline_fit, reasons}; `score` = band*BAND + within
    scaled to stay inside the band, so a plain sort on score honours the banding."""
    import math
    ev = row.get("evidence") or {}
    art = row.get("artifacts") or {}
    li = row.get("linkedin") or {}
    reasons: list[str] = []
    rel = float(row.get("relevance") if row.get("relevance") is not None else (row.get("match_pct") or 0) / 100.0)
    rel = max(0.0, min(1.0, rel))
    band = int(math.floor(rel / BAND + 1e-9))     # epsilon: 0.70/0.05 is 13.999… in floats
    if row.get("match_pct"):
        reasons.append(f"relevance {int(row['match_pct'])}%")
    within = 0.0
    if ev.get("corroborated_keys"):
        within += 0.30; reasons.append("corroborated " + ", ".join(ev["corroborated_keys"][:2]))
    elif ev.get("consistent_keys") or (ev.get("calibration") or {}).get("band") == "consistent":
        within += 0.15; reasons.append("consistent affiliation (self-stated claims agree with independent signals)")
    if "artifact_backed" in (ev.get("types") or []):
        within += 0.12; reasons.append("artifact-backed capability")
    total = int(art.get("total") or 0)
    counts = art.get("counts") or {}
    if total:
        within += min(0.15, 0.05 * math.log1p(total))
        reasons.append("footprint: " + ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(counts.items())))
    items = art.get("items") or []
    terms = _facet_terms(brief_facets)
    if terms and items:
        hits = []
        for it in items:
            hay = " ".join([it.get("title") or "", it.get("venue") or ""]).lower().replace("_", " ").replace("-", " ")
            if any(t in hay for t in terms):
                hits.append(it.get("title") or "")
        if hits:
            within += min(0.20, 0.07 * len(hits))
            reasons.append(f"{len(hits)} artifact{'s' if len(hits) != 1 else ''} match the brief ({', '.join(h[:28] for h in hits[:3])})")
    wants_senior = any(str(v).lower() in _SENIOR_WORDS for v in (brief_facets or {}).get("seniority") or [])
    if wants_senior and items:
        first = sum(1 for it in items if it.get("role") == "first_author")
        cites = sum(_stat_number(it.get("stat") or "") for it in items if "citation" in (it.get("stat") or ""))
        stars = sum(1 for it in items if (it.get("stat") or "").endswith("★") and _stat_number(it.get("stat")) >= 10)
        orgs = int(counts.get("org") or 0)
        sen = min(0.20, 0.04 * first + 0.06 * (cites >= 100) + 0.06 * (cites >= 1000) + 0.03 * stars + 0.03 * (orgs > 0))
        if sen:
            within += sen
            bits = []
            if first: bits.append(f"{first} first-author paper{'s' if first != 1 else ''}")
            if cites: bits.append(f"{cites:,} citations")
            if stars: bits.append(f"{stars} starred repo{'s' if stars != 1 else ''}")
            if orgs: bits.append("org membership")
            reasons.append("seniority evidence: " + ", ".join(bits))
    newest = str(art.get("newest") or "")[:4]
    if newest.isdigit():
        import datetime
        if datetime.date.today().year - int(newest) <= 2:
            within += 0.06; reasons.append(f"active recently (newest artifact {newest})")
    fit = li.get("headline_fit")
    if fit is not None:
        within += 0.20 * float(fit); reasons.append(f"LinkedIn headline fits the brief {int(float(fit) * 100)}%")
    if art.get("scanned") and not total:
        within -= 0.05; reasons.append("scanned: no public artifacts found")
    within = max(0.0, min(1.0, within))
    score = band * BAND + within * (BAND * 0.98)     # never crosses into the next band
    return {"score": round(score, 4), "band": band, "relevance": round(rel, 3), "within": round(within, 3),
            "headline_fit": fit, "reasons": reasons}


def rank_sort_key(row: dict) -> tuple:
    rr = row.get("rank_read") or {}
    return (-int(rr.get("band") or 0), -float(rr.get("within") or 0.0), -float(rr.get("relevance") or 0.0))
