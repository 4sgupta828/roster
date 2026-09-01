#!/usr/bin/env python
"""HELD-OUT routing eval for the native Q&A router (flag ROSTER_QA_ROUTER) — Rule 16.

None of these questions appear in the router prompt. Each case pins the EXPECTED route (a set when
two routes are defensible) plus, where it matters, required entities — the design's acceptance axis
"route by intent, not endpoint accident". Adversarial cases include discovery-vs-dossier, insights-
vs-discovery, connection phrasing variants, and JD-shaped pastes.

Run (in-container, needs an LLM key; ~24 small calls ≈ well under $0.05):
    railway ssh --service roster-api "python scripts/eval_qa_routes.py"
Local dry list (free):  python scripts/eval_qa_routes.py --dry
Exits non-zero when accuracy < the gate (0.8), printing every miss.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

_JD = """Analyze this job description:

About the role
We're hiring a Staff Platform Engineer to own our ingestion pipelines.

Requirements
- 7+ years building distributed systems
- Kafka, Postgres, Kubernetes
- Experience operating multi-region infrastructure

Responsibilities
- Design and run the event backbone
- Mentor senior engineers
""" + "…\n" * 30

# (question, {allowed routes}, required-entity substrings)
CASES: list[tuple[str, set[str], list[str]]] = [
    ("find senior ML engineers in Toronto", {"indexed_people_discovery"}, []),
    ("staff+ infra people who have worked at Stripe", {"indexed_people_discovery"}, []),
    ("who is Guido van Rossum?", {"person_dossier"}, ["Guido"]),
    ("tell me about Mira Murati's background", {"person_dossier"}, ["Murati"]),
    ("what is Databricks like as a place to work for engineers?", {"company_hiring"}, ["Databricks"]),
    ("is Anthropic hiring infrastructure engineers right now?",
     {"company_hiring", "indexed_job_search"}, ["Anthropic"]),
    (_JD, {"jd_analysis"}, []),
    ("how is Sam Altman connected to Y Combinator?", {"connection_path"}, ["Altman"]),
    ("who connects OpenAI and Stripe?", {"connection_path"}, []),
    ("who has Andrej Karpathy worked with?", {"connection_path", "person_dossier"}, ["Karpathy"]),
    ("how many people in the index are ex-Google?", {"insights"}, []),
    ("top 10 companies by open backend roles", {"insights", "indexed_job_search"}, []),
    ("breakdown of seniority across indexed ML people", {"insights"}, []),
    ("remote senior backend roles at fintech companies", {"indexed_job_search"}, []),
    ("open ML compiler jobs in the Bay Area", {"indexed_job_search"}, []),
    ("explain the market for AI infra recruiters", {"general_professional_qa"}, []),
    ("what's the difference between a staff and principal engineer?",
     {"general_professional_qa"}, []),
    # adversarial: named person phrased like discovery
    ("find out everything about Jeff Dean", {"person_dossier"}, ["Jeff Dean"]),
    # adversarial: count phrased like discovery
    ("how many rust engineers do we have in Berlin?", {"insights", "indexed_people_discovery"}, []),
    # adversarial: company that is also a common word
    ("who is Figma's head of engineering?", {"person_dossier", "company_hiring",
                                             "general_professional_qa"}, []),
    # adversarial: relationship phrasing without 'connected'
    ("did Patrick Collison and John Collison found Stripe together?",
     {"connection_path", "general_professional_qa"}, []),
    # adversarial: JD by URL
    ("what does this role need? https://boards.greenhouse.io/stripe/jobs/123",
     {"jd_analysis"}, []),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="list cases, no LLM calls, no cost")
    args = ap.parse_args()
    if args.dry:
        for q, routes, ents in CASES:
            print(f"[case] {q[:70]!r:74} → {'/'.join(sorted(routes))}")
        print(f"{len(CASES)} cases (live run ≈ {len(CASES)} small LLM calls)")
        return 0
    sys.path.insert(0, "apps")
    from roster_kernel.providers.base import resolve_mode
    from roster_kernel.runtime.build import build_llm
    from api.qa_router import classify_qa_route
    llm = build_llm(mode=resolve_mode())
    hits, misses = 0, []
    for q, routes, ents in CASES:
        r = await classify_qa_route(q, llm)
        ok = r.route in routes
        if ok and ents:
            joined = " ".join(r.entities)
            ok = all(any(e.lower() in x.lower() for x in r.entities) or e.lower() in joined.lower()
                     for e in ents)
        hits += ok
        mark = "✓" if ok else "✗"
        print(f"{mark} {q[:64]!r:68} → {r.route:26} conf={r.confidence} ents={r.entities}")
        if not ok:
            misses.append((q, r.route, sorted(routes)))
    acc = hits / len(CASES)
    print(f"\nrouting accuracy: {hits}/{len(CASES)} = {acc:.0%}  (gate: 80%)")
    for q, got, want in misses:
        print(f"  MISS: {q[:70]!r} got={got} want={want}")
    return 0 if acc >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
