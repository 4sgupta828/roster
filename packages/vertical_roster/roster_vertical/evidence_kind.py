"""Deterministic evidence-kind classifier — facets → the `authority.py` tech pyramid.

STRUCTURAL, not semantic (Rule 18): maps computable per-source metadata the connectors
already emit (`source_kind`, `form_type`, `grant_status`, `is_peer_reviewed`) onto the
tier vocabulary `TechAuthorityPolicy` ranks. It makes NO judgment about meaning — it reads
structured tags a source published about itself. Unknown/ambiguous → "" (rank 0), so a
missing tag never boosts NOR demotes below the retrieval-relevance baseline (fail-safe).

Key discipline: a patent APPLICATION is not a granted patent (intent, not fact) → it does
not get the primary tier; and social/news sentiment lands at the lowest tier so it can
never be presented as an established fact.
"""
from __future__ import annotations


def classify(source_key: str, facets: dict[str, str] | None, title: str = "", text: str = "") -> str:
    """Return a `authority.py` evidence-kind key (or "" when unclassifiable)."""
    f = facets or {}
    sk = (source_key or "").lower()
    src_kind = (f.get("source_kind") or "").lower()

    # 1) PRIMARY — audited/legal record. SEC filings and GRANTED patents.
    if src_kind == "filing" or sk in ("edgar", "sec"):
        return "primary_filing"
    if src_kind == "patent":
        # a GRANTED patent is a legal record (primary); a pending APPLICATION is intent.
        granted = str(f.get("grant_status") or f.get("is_granted") or "").lower()
        return "primary_filing" if granted in ("granted", "true", "1", "yes") else "technical_signal"

    # 2) VERIFIED STRUCTURED — peer-reviewed papers, funding records, benchmarks, curated reference.
    if src_kind == "funding" or src_kind == "benchmark":
        return "verified_structured"
    if src_kind == "paper":
        reviewed = str(f.get("is_peer_reviewed") or "").lower()
        return "verified_structured" if reviewed in ("true", "1", "yes") else "technical_signal"
    # Curated structured reference (Wikidata: founder/inception/ownership/ticker) — structured facts a
    # source published about itself, above press but below a filing. Previously UNMAPPED → silently rank 0.
    # YC company directory joins this tier: a curated public startup-directory profile (population
    # seed + founders) is structured reference — above press, below a filing — same as Wikidata.
    if src_kind == "reference" or sk in ("wikidata", "yc"):
        return "verified_structured"

    # 3) TECHNICAL SIGNAL — preprints, open-source + model-hub traction.
    if src_kind in ("preprint",) or sk == "arxiv":
        return "technical_signal"
    if src_kind == "code" or sk == "github":
        return "technical_signal"
    # Hugging Face model-hub adoption (downloads/likes) — a real but self-reported adoption signal.
    if src_kind == "model" or sk == "huggingface":
        return "technical_signal"
    # Company engineering blogs (Netflix/Cloudflare/Meta Eng/…) — a self-reported corporate account of
    # how an org builds. Genuinely technical, but written BY the org ABOUT its own tech (self-promotion
    # bias), so it sits at the SAME self-reported tier as github/preprints — deliberately BELOW a named
    # individual's essay (expert_analysis) and never able to outrank fact-checked press on the org's own
    # tech. STRUCTURAL: reads the source_kind the connector stamped, judges nothing.
    if src_kind == "corp_eng" or sk == "eng_blog":
        return "technical_signal"

    # 3b) EXPERT ANALYSIS — a NAMED expert's interpretation/foresight (essays, newsletters, recorded
    # expert/practitioner discussion). Opinion above an unreviewed preprint, below fact-checked press;
    # never controlling. STRUCTURAL: reads the source_kind the connector stamped, judges nothing.
    if src_kind in ("essay", "newsletter", "expert", "podcast") \
            or sk in ("expert_feed", "podcast"):
        return "expert_analysis"

    # 4) ANALYSIS — reputable press / analyst notes.
    if src_kind == "news" or sk in ("reuters", "bloomberg", "ft"):
        return "analysis"

    # 5) SENTIMENT SIGNAL — social / forum / tone. Lowest tier; never controlling.
    if src_kind in ("social", "sentiment") or sk in ("hackernews", "gdelt", "reddit", "stackoverflow"):
        return "sentiment_signal"

    # 6) OPEN-WEB PROVENANCE FALLBACK (flag ROSTER_AUTHORITY_BASIS). A screened open-web hit carries NO
    # source_kind (the connector never stamped one), so it would fall through to "" (rank 0) and the
    # authority-basis partition would sink even a good non-whitelisted source. The open-web quality
    # screen stamps a COARSE role facet `web_role` on kept hits; map it here to the SAME tier an
    # equivalent source_kind would earn, so a real company/analysis/expert page gets a real tier while
    # social/aggregator stay low. STRUCTURAL (Rule 18): reads a stamped role, judges nothing. Only fires
    # when source_kind is EMPTY and `web_role` is present — so an unflagged (unstamped) hit is unchanged.
    if not src_kind:
        role = (f.get("web_role") or "").lower()
        if role == "official":
            return "technical_signal"        # company/official page → tier 2 (as corp_eng)
        if role == "independent_analysis":
            return "analysis"                # reputable independent coverage → tier 4 (as news)
        if role == "expert_opinion":
            return "expert_analysis"         # named expert essay/newsletter → tier 3 (as essay)
        if role == "social":
            return "sentiment_signal"        # social post → tier 1
        # "aggregator" (or unknown) → fall through to "" (rank 0), same as an unstamped open-web hit.

    return ""   # unknown → rank 0 (never boosts, never demotes)


def recency_year(facets: dict[str, str] | None) -> int | None:
    """Extract a 4-digit filing/publication year from facets (structural), or None."""
    raw = (facets or {}).get("year")
    if raw is None:
        return None
    s = str(raw).strip()[:4]
    return int(s) if s.isdigit() and len(s) == 4 else None
