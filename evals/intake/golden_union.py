"""GOLDEN UNION (docs/specs/guided-intake.md §12.10): a frozen brief with frozen rows and HUMAN labels, run with
every paired eval to catch judge drift. This script BUILDS the candidate file from a paired run — the rows one
scenario's two arms surfaced, with the eval judge's verdicts as PROPOSED labels — for the owner to confirm or edit
by hand (`"confirmed": true` once labelled). Nothing here is a label until a person says so.

  python evals/intake/golden_union.py --run evals/intake/runs/paired-<ts>.json --scenario hiring_field_vs_specialty
Then: python evals/intake/paired_eval.py --golden evals/intake/golden_union.json   (drift check only, one judge call)
"""
from __future__ import annotations

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "golden_union.json"))
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    run = json.load(open(args.run))
    rec = next((r for r in run["results"] if r["id"] == args.scenario), None)
    if not rec or "union_rows" not in rec:
        raise SystemExit("that run carries no union rows for the scenario (runs made before union_rows were recorded cannot seed a golden set)")
    rows = rec["union_rows"][: args.n]
    golden = {"scenario": args.scenario, "kind": rec["kind"], "brief": rec["brief_full"], "contract": rec.get("contract") or {},
              "confirmed": False, "agreement_baseline": None,
              "note": "PROPOSED labels come from the eval judge; a person confirms or edits `label` per row, then sets confirmed=true. "
                      "Add adversarial rows by hand (wrong company, wrong level, adjacent specialty, stale role, keyword-only) with label 'no'.",
              "rows": [{**r, "label": (rec.get("verdicts") or {}).get(str(r.get("id")), {}).get("fit"), "proposed_why": (rec.get("verdicts") or {}).get(str(r.get("id")), {}).get("why", "")} for r in rows]}
    json.dump(golden, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}: {len(golden['rows'])} rows, labels PROPOSED (confirm by hand)")


if __name__ == "__main__":
    main()
