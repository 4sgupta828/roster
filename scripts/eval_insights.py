#!/usr/bin/env python
"""Held-out eval for the grounded Insights Q&A (Rule 16 — LLM behavior needs a held-out gate).

Runs real questions (NONE of which appear in any prompt/few-shot) through the live compiler + aggregation
against the prod index, and asserts:
  - the compiler routes to the right target/group_by (or abstains on out-of-facet asks),
  - GROUNDING: every integer in the narrative also appears in the code-computed rows (no fabricated stat),
  - abstain questions do NOT hit the DB / return grounded=False,
  - a skill-grouped question carries the sparsity caveat.

Run in-container (needs ROSTER_CORPUS_DSN + the LLM):
    railway ssh --service roster-api "python scripts/eval_insights.py"
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

# (question, expected target, acceptable group_by set OR None for abstain)
CASES = [
    ("which companies employ the most senior software engineers?", "people", {"company"}),
    ("what is the seniority breakdown among machine learning people?", "people", {"seniority"}),
    ("where are data scientists concentrated?", "people", {"metro", "country", "state"}),
    ("what are the most common functions in the index?", "people", {"function", "role"}),
    ("which companies have the most open roles right now?", "jobs", {"company"}),
    ("what is the average salary for backend engineers?", "abstain", None),
    ("how has hiring changed over the past year?", "abstain", None),
    ("which companies employ the most people who know kubernetes?", "people", {"company"}),  # skill filter
]


def _ints(s: str) -> set[int]:
    # integers in prose (strip separators), ignoring tiny ordinals/years
    return {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", s or "")}


async def main() -> None:
    from api.people_population import parse_analytical_query, answer_roster_insights
    from api.claimgraph_tech import make_tech_claim_store
    from roster_kernel.runtime.build import build_llm
    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ROSTER_CORPUS_DSN required"); sys.exit(2)
    store = make_tech_claim_store(dsn)
    llm = build_llm(mode="live")
    passed = failed = 0
    for q, want_target, want_groups in CASES:
        spec = await parse_analytical_query(q, llm)
        tgt = getattr(spec, "target", "?")
        ok = True; why = []
        if want_target == "abstain":
            if tgt != "abstain":
                ok = False; why.append(f"expected abstain, got target={tgt} group_by={spec.group_by}")
            res = await answer_roster_insights(question=q, tenant_id="demo", store=store, llm=llm)
            if res.get("grounded") or not res.get("abstain"):
                ok = False; why.append("abstain question did not abstain in answer")
        else:
            if tgt != want_target:
                ok = False; why.append(f"target={tgt}, expected {want_target}")
            if want_groups and spec.group_by not in want_groups:
                ok = False; why.append(f"group_by={spec.group_by}, expected one of {sorted(want_groups)}")
            res = await answer_roster_insights(question=q, tenant_id="demo", store=store, llm=llm)
            rows = res.get("rows") or []
            if res.get("grounded"):
                # GROUNDING: every integer in the narrative must be a real computed count.
                allowed = {r["n"] for r in rows} | set(range(0, len(rows) + 2))  # counts + small ordinals
                stray = _ints(res.get("narrative", "")) - allowed
                # allow the coverage denominators too
                cov = res.get("coverage_basis") or {}
                allowed |= _ints(str(cov.get("population_statement", "")))
                stray = _ints(res.get("narrative", "")) - allowed
                if stray:
                    ok = False; why.append(f"UNGROUNDED numbers in narrative: {sorted(stray)[:5]}")
            # skill-conditioned question must carry a caveat
            if "kubernetes" in q and res.get("grounded") and not (res.get("caveats")):
                ok = False; why.append("skill question missing sparsity caveat")
        print(f"{'PASS' if ok else 'FAIL'}  {q[:60]:60}  target={tgt} group_by={getattr(spec,'group_by','')}")
        for w in why:
            print(f"       - {w}")
        passed += ok; failed += (not ok)
    print(f"\n=== {passed}/{passed+failed} passed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
