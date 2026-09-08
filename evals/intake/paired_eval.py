"""PAIRED EVAL for contract search step 2 (docs/specs/guided-intake.md §12.10): every scenario that reaches a
hand-off runs BOTH arms on prod — BASELINE (the single ratified contract) and MERGED (recipes fused + the blind
judge) — then the UNION of both top-20s is graded ONCE, blind, against the brief (the user's own words). Report:
prec10 / prec20 per arm, the lift, `missed` (judged-yes rows in the union that an arm's top-20 lacks), the judge's
yes / partial / no rates, seconds per arm. The GATE (§12.10): merged ≥ baseline on ≥ 80 % of scenarios, no scenario
worse by > 0.10 prec10, missed ≈ 0 for merged, p90 added latency ≤ 5 s.

Spend ≈ the intake battery (+ one judge call per scenario ≈ $0.004). Throwaway accounts only (`@roster.test`);
sweep them afterwards with sweep_throwaways.py in the container.

  python evals/intake/paired_eval.py [--only a,b] [--base URL]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_intake_eval import Run, post  # noqa: E402


def load_scenarios(path: str) -> list[dict]:
    return [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]

PARTIAL, WEAK_YES = 0.4, 0.8


def _rid(r: dict) -> str:
    return str(r.get("id") or r.get("entity_id") or "")


def _judge_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({"id": _rid(r), "title": r.get("title"), "company": r.get("company"), "location": r.get("location"), "blurb": r.get("blurb"),
                    "facets": {k: v for k, v in (r.get("facets") or {}).items() if isinstance(v, list)}})
    return out


def _prec(rows: list[dict], verdicts: dict, k: int) -> float:
    tot = 0.0
    for r in rows[:k]:
        v = (verdicts.get(_rid(r)) or {}).get("fit")
        ev = [str(x) for x in ((r.get("facets") or {}).get("evidence") or [])]
        weak = not [e for e in ev if e not in ("", "self_stated", "profile")]
        tot += (WEAK_YES if weak else 1.0) if v == "yes" else (PARTIAL if v == "partial" else 0.0)
    return round(tot / max(k, 1), 3)


def handoff(run: Run, c: dict, mode: str) -> tuple[list[dict], float, dict | None]:
    c2 = {**c, "merge": {"mode": mode, "off": []}}
    t0 = time.time()
    if run.ready.get("direction") == "job":
        d = post(run.base, "/jobs", {"question": c.get("text") or "roles", "tenant_id": "demo", "surface": "jobs", "contract": c2, "country": "us"}, run.token)
        rows, merge = list(d.get("jobs") or []), d.get("merge")
    else:
        d = post(run.base, "/research", {"question": c.get("text") or "people", "tenant_id": "demo", "surface": "people", "contract": c2, "country": "us"}, run.token)
        rows, merge = list(d.get("people_rows") or []), (d.get("facet_nav") or {}).get("merge")
    return rows, round(time.time() - t0, 1), merge


def golden_check(base: str, path: str) -> None:
    """Judge the frozen rows (shuffled per run inside the endpoint) and report agreement with the human labels.
    A confirmed set with a recorded baseline fails the run when agreement shifts by more than 5 points."""
    g = json.load(open(path))
    rows = [{k: v for k, v in r.items() if k not in ("label", "proposed_why")} for r in g["rows"]]
    jd = post(base, "/search/judge", {"kind": g["kind"], "brief": g["brief"], "rows": rows, "provider": "alt", "contract": g.get("contract") or {}})
    v = jd.get("verdicts") or {}
    labelled = [r for r in g["rows"] if r.get("label")]
    agree = sum(1 for r in labelled if (v.get(str(r["id"])) or {}).get("fit") == r["label"])
    pct = round(100 * agree / max(len(labelled), 1), 1)
    status = "PROPOSED labels — not a drift check until confirmed" if not g.get("confirmed") else ("baseline recorded" if g.get("agreement_baseline") is None else
              ("DRIFT — fail" if abs(pct - float(g["agreement_baseline"])) > 5 else "ok"))
    print(f"golden union: {len(labelled)} labelled rows · judge agrees on {agree} ({pct} %) · {status}")
    for r in labelled:
        got = (v.get(str(r["id"])) or {}).get("fit")
        if got != r["label"]:
            print(f"   disagree {r['id']}: label {r['label']} · judge {got} — {(v.get(str(r['id'])) or {}).get('why', '')}")
    if g.get("confirmed") and g.get("agreement_baseline") is None:
        g["agreement_baseline"] = pct; json.dump(g, open(path, "w"), indent=1); print("   agreement baseline recorded")
    if g.get("confirmed") and g.get("agreement_baseline") is not None and abs(pct - float(g["agreement_baseline"])) > 5:
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("ROSTER_EVAL_BASE", "https://roster-api-production-3405.up.railway.app"))
    ap.add_argument("--only", default="")
    ap.add_argument("--scenarios", default=os.path.join(HERE, "scenarios.jsonl"))
    ap.add_argument("--golden", default="", help="golden_union.json: judge-drift check only (one judge call)")
    args = ap.parse_args()
    if args.golden:
        return golden_check(args.base, args.golden)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    results = []
    t_all = time.time()
    live_url = ""
    try:   # a live posting URL for the link scenario
        d = post(args.base, "/search/evaluate", {"contract": {"kind": "job", "text": "backend engineer", "must": {"field": ["software"]}, "limit": 5}})
        live_url = next((r.get("url") for r in d.get("rows") or [] if str(r.get("url") or "").startswith("http")), "")
    except Exception:   # noqa: BLE001
        pass
    for sc in load_scenarios(args.scenarios):
        if only and sc["id"] not in only:
            continue
        if any("improve" in st for st in sc["steps"]) or (sc.get("expect") or {}).get("no_draft"):
            continue
        run = Run(args.base, sc, live_url)
        try:
            run.seed(); run.drive()
        except urllib.error.HTTPError as e:
            print(f"SKIP  {sc['id']:38s} intake HTTP {e.code}"); run.cleanup(); continue
        if not (run.ready and run.ready.get("contract")):
            print(f"SKIP  {sc['id']:38s} no hand-off"); run.cleanup(); continue
        c = run.ready["contract"]
        try:
            base_rows, base_s, _ = handoff(run, c, "single")
            merged_rows, merged_s, merge = handoff(run, c, "merged")
        except urllib.error.HTTPError as e:
            print(f"FAIL  {sc['id']:38s} hand-off HTTP {e.code}"); run.cleanup(); continue
        union, seen = [], set()
        for r in base_rows[:20] + merged_rows[:20]:
            if _rid(r) and _rid(r) not in seen:
                seen.add(_rid(r)); union.append(r)
        brief = str(c.get("text") or "")
        kind = "job" if run.ready.get("direction") == "job" else "person"
        try:
            # the eval's judge is a DIFFERENT provider from the in-product judge (spec §12.11: agreement bias)
            jd = post(args.base, "/search/judge", {"kind": kind, "brief": brief, "rows": _judge_rows(union), "provider": "alt",
                                                   "contract": {k: c.get(k) for k in ("must", "prefer", "center")}}, run.token)
        except urllib.error.HTTPError as e:
            print(f"FAIL  {sc['id']:38s} judge HTTP {e.code}"); run.cleanup(); continue
        verdicts = jd.get("verdicts") or {}
        yes_ids = {rid for rid, v in verdicts.items() if v.get("fit") == "yes"}
        base_top = {_rid(r) for r in base_rows[:20]}; merged_top = {_rid(r) for r in merged_rows[:20]}
        rec = {"id": sc["id"], "kind": kind, "brief": brief[:160], "union": len(union), "graded": len(verdicts),
               "rates": {k: sum(1 for v in verdicts.values() if v.get("fit") == k) for k in ("yes", "partial", "no")},
               "baseline": {"prec10": _prec(base_rows, verdicts, 10), "prec20": _prec(base_rows, verdicts, 20), "missed": len(yes_ids - base_top), "secs": base_s, "rows": len(base_rows)},
               "merged": {"prec10": _prec(merged_rows, verdicts, 10), "prec20": _prec(merged_rows, verdicts, 20), "missed": len(yes_ids - merged_top), "secs": merged_s, "rows": len(merged_rows),
                          "recipes": [{k: r.get(k) for k in ("name", "pool", "evaluated", "head_fits", "prec10")} for r in ((merge or {}).get("recipes") or [])],
                          "fits": (merge or {}).get("fits"), "weak": (merge or {}).get("weak"), "judge_error": (merge or {}).get("judge_error"), "timings": (merge or {}).get("timings")},
               "top": {arm: [(_rid(r), (verdicts.get(_rid(r)) or {}).get("fit"), str(r.get("title") or r.get("name") or "")[:40], (verdicts.get(_rid(r)) or {}).get("why", ""), r.get("fit"), r.get("fit_why", ""))
                             for r in rows_[:10]] for arm, rows_ in (("baseline", base_rows), ("merged", merged_rows))}}
        rec["brief_full"] = brief; rec["contract"] = {k: c.get(k) for k in ("must", "prefer", "center")}
        rec["verdicts"] = verdicts; rec["union_rows"] = _judge_rows(union)
        rec["lift10"] = round(rec["merged"]["prec10"] - rec["baseline"]["prec10"], 3)
        rec["added_secs"] = round(merged_s - base_s, 1)
        results.append(rec)
        print(f"{'UP  ' if rec['lift10'] > 0 else 'SAME' if rec['lift10'] == 0 else 'DOWN'}  {sc['id']:36s} prec10 {rec['baseline']['prec10']:.2f} → {rec['merged']['prec10']:.2f}  "
              f"prec20 {rec['baseline']['prec20']:.2f} → {rec['merged']['prec20']:.2f}  missed {rec['baseline']['missed']} → {rec['merged']['missed']}  "
              f"+{rec['added_secs']}s  recipes {len(rec['merged']['recipes'])} fits {rec['merged']['fits']}{' WEAK' if rec['merged']['weak'] else ''}"
              + (f"  judge_error {rec['merged']['judge_error']}" if rec['merged']['judge_error'] else ""), flush=True)
        run.cleanup()
    if not results:
        print("no paired results"); return
    n = len(results)
    ge = sum(1 for r in results if r["lift10"] >= 0); worse = [r["id"] for r in results if r["lift10"] < -0.10]
    missed = sum(r["merged"]["missed"] for r in results)
    secs = sorted(r["added_secs"] for r in results); p90 = secs[min(len(secs) - 1, int(0.9 * (len(secs) - 1)))]
    mean = lambda k, arm: round(sum(r[arm][k] for r in results) / n, 3)
    gate = {"merged_ge_baseline_share": round(ge / n, 2), "worse_by_over_0_10": worse, "merged_missed_total": missed, "added_p90_secs": p90,
            "pass": (ge / n >= 0.8) and not worse and missed == 0 and p90 <= 5}
    summary = {"n": n, "prec10": {"baseline": mean("prec10", "baseline"), "merged": mean("prec10", "merged")}, "prec20": {"baseline": mean("prec20", "baseline"), "merged": mean("prec20", "merged")},
               "missed": {"baseline": sum(r["baseline"]["missed"] for r in results), "merged": missed}, "gate": gate}
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    out = os.path.join(HERE, "runs", f"paired-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump({"base": args.base, "secs": round(time.time() - t_all), "summary": summary, "results": results}, open(out, "w"), indent=1)
    print(f"\n{n} paired · prec10 {summary['prec10']['baseline']:.2f} → {summary['prec10']['merged']:.2f} · prec20 {summary['prec20']['baseline']:.2f} → {summary['prec20']['merged']:.2f} "
          f"· missed {summary['missed']['baseline']} → {summary['missed']['merged']} · merged ≥ baseline on {gate['merged_ge_baseline_share']:.0%} · worse>0.10: {worse or 'none'} "
          f"· added p90 {p90}s · GATE {'PASS' if gate['pass'] else 'FAIL'} → {out}")


if __name__ == "__main__":
    main()
