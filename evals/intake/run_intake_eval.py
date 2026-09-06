"""Guided-intake EVAL (docs/specs/guided-intake.md §7): persona-scripted conversations against a live API, scored on
CONTENT — direction, artifact path, over-asking, repeats, confabulated constraints, the intent text, draft
citations and peer relevance, empty-result diagnosis, hand-off rows, improve-and-save. No browser; the FE is
covered by the Playwright smokes.

    python evals/intake/run_intake_eval.py --base https://roster-api-production-3405.up.railway.app [--only id,id]

Seeded scenarios (résumé on file, saved JDs) register a `@roster.test` throwaway through /auth/register and delete
their rows at the end (never the owner's account). Spend: ≈ 60 small model calls + two drafts ≈ $0.15 per run."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def post(base: str, path: str, body: dict, token: str = "", timeout: int = 300) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"content-type": "application/json", **({"x-roster-token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(base: str, path: str, token: str = "") -> dict:
    req = urllib.request.Request(base + path, headers={**({"x-roster-token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _auto_value(q: dict, prefer: dict, decline_all: bool):
    """A scripted persona answers the question the intake asked: a preferred value when the persona has one for
    that question, else the first option, else a decline."""
    name = q.get("name"); key = q.get("key") or name
    if decline_all:
        return "__decline__"
    for k in (name, key):
        if k in prefer:
            v = prefer[k]
            return v
    opts = q.get("options") or []
    if opts:
        return opts[0][0]
    return "__decline__"


class Run:
    def __init__(self, base: str, sc: dict, live_url: str):
        self.base, self.sc, self.live_url = base, sc, live_url
        self.state = None; self.turns = []; self.token = ""; self.email = ""
        self.asked: list[str] = []; self.questions: list[dict] = []; self.ready = None; self.draft_first = None; self.draft_last = None
        self.context_q = False; self.direction_q = False; self.which_jd = False; self.improved = None; self.handoff = None
        self.failures: list[str] = []; self.notes: list[str] = []

    # ---- seeding ----
    def seed(self):
        if not (self.sc.get("seed_resume") or self.sc.get("seed_briefs")):
            return
        self.email = f"intake-eval-{int(time.time()*1000)}@roster.test"
        reg = post(self.base, "/auth/register", {"email": self.email, "name": "Intake Eval", "password": "Eval-pass-1234"})
        self.token = reg.get("token") or ""
        if self.sc.get("seed_resume"):
            post(self.base, "/me/briefs", {"kind": "resume", "title": "seed", "text": self.sc["seed_resume"], "make_active": True}, self.token)
        for b in self.sc.get("seed_briefs") or []:
            post(self.base, "/me/briefs", {"kind": "jd", "title": b["title"], "text": b["text"]}, self.token)

    def cleanup(self):
        if not self.token:
            return
        try:
            for b in get(self.base, "/me/briefs", self.token).get("briefs") or []:
                req = urllib.request.Request(self.base + f"/me/briefs/{b['id']}", method="DELETE", headers={"x-roster-token": self.token})
                urllib.request.urlopen(req, timeout=60).read()
        except Exception:   # noqa: BLE001
            pass
        self.notes.append(f"throwaway {self.email} (delete the user row in cleanup)")

    # ---- driving ----
    def step(self, **body):
        body["state"] = self.state
        out = post(self.base, "/intake/step", body, self.token)
        self.state = out.get("state") or self.state
        q = out.get("question")
        if q:
            self.questions.append(q)
            if q.get("kind") == "direction":
                self.direction_q = True
            elif q.get("kind") == "draft_context":
                self.context_reasks = getattr(self, "context_reasks", 0) + (1 if self.context_q else 0)
                self.context_q = True
            elif q.get("kind") == "draft":
                if self.draft_first is None:
                    self.draft_first = q.get("draft")
                self.draft_last = q.get("draft")
            elif q.get("kind") == "artifact" and any(str(o[0]).startswith("brief:") for o in (q.get("options") or [])):
                self.which_jd = True
            elif q.get("kind") in ("item", "key"):
                self.asked.append(f"{q['kind']}:{q['name']}")
        if out.get("stage") == "ready":
            self.ready = out.get("ready")
        self.turns.append(out)
        return out

    def drive(self):
        for st in self.sc["steps"]:
            if self.ready:
                break
            if "message" in st:
                msg = st["message"].replace("__LIVE_JOB_URL__", self.live_url or "")
                self.step(message=msg)
            elif "direction" in st:
                self.step(message="", direction=st["direction"])
            elif "answer" in st:
                self.step(answer=st["answer"])
            elif "search_now" in st:
                self.step(search_now=True)
            elif "pick_brief" in st:
                q = self.turns[-1].get("question") or {}
                opt = next((o for o in (q.get("options") or []) if str(o[1]).startswith(st["pick_brief"])), None)
                if not opt:
                    self.failures.append(f"no saved-JD chip for {st['pick_brief']!r}: {[o[1] for o in q.get('options') or []]}")
                    return
                self.step(answer={"name": "jd", "value": opt[0]})
            elif "inject_must" in st:
                k = (self.state or {}).get("kernel") or {}
                k.setdefault("contract", {}).setdefault("must", {}).update(st["inject_must"])
            elif "improve" in st:
                self.improved = post(self.base, "/intake/improve", {"state": self.state}, self.token)
            elif st.get("auto"):
                prefer = st.get("prefer") or {}; n = 0; cap = int(st.get("max_answers") or 12)
                while not self.ready and n < cap:
                    q = self.turns[-1].get("question") or {}
                    if q.get("kind") not in ("item", "key"):
                        break
                    v = _auto_value(q, prefer, bool(st.get("decline_all")))
                    self.step(answer={"name": q["name"], "value": v}); n += 1
        # the hand-off, when ready
        if self.ready and self.ready.get("contract"):
            c = self.ready["contract"]
            try:
                if self.ready.get("direction") == "job":
                    d = post(self.base, "/jobs", {"question": c.get("text") or "roles", "tenant_id": "demo", "surface": "jobs", "contract": c, "country": "us"}, self.token)
                    self.handoff = list(d.get("jobs") or [])
                else:
                    d = post(self.base, "/research", {"question": c.get("text") or "people", "tenant_id": "demo", "surface": "people", "contract": c, "country": "us"}, self.token)
                    self.handoff = list(d.get("people_rows") or [])
            except urllib.error.HTTPError as e:
                self.failures.append(f"hand-off HTTP {e.code}")

    # ---- scoring ----
    def score(self):
        e = self.sc.get("expect") or {}
        k = ((self.state or {}).get("kernel") or {})
        c = (self.ready or {}).get("contract") or k.get("contract") or {}
        text = str(c.get("text") or "")
        F = self.failures.append
        if "direction" in e and k.get("direction") != e["direction"]:
            F(f"direction {k.get('direction')!r} != {e['direction']!r}")
        if e.get("asks_direction_first") and not self.direction_q:
            F("did not ask the direction on an ambiguous opening")
        if "artifact_source" in e and (k.get("artifact") or {}).get("source") != e["artifact_source"]:
            F(f"artifact source {(k.get('artifact') or {}).get('source')!r} != {e['artifact_source']!r}")
        if "asked_max" in e and len(self.asked) > e["asked_max"]:
            F(f"asked {len(self.asked)} > {e['asked_max']}: {self.asked}")
        if len(self.asked) != len(set(self.asked)):
            F(f"repeated a question: {self.asked}")
        for it in e.get("never_asks_present") or []:
            if f"item:{it}" in self.asked and k.get("checklist", {}).get(it) in ("present", "answered"):
                F(f"asked {it!r} although the artifact stated it")
        for it in e.get("asks_items_include") or []:
            if f"item:{it}" not in self.asked and f"key:{it}" not in self.asked:
                F(f"never asked {it!r}")
        for w in e.get("text_contains") or []:
            if w.lower() not in text.lower():
                F(f"search text lacks {w!r}: {text[:120]!r}")
        for w in e.get("text_excludes") or []:
            if w.lower() in text.lower():
                F(f"search text carries the artifact ({w!r}): {text[:120]!r}")
        if len(text) > 400:
            F(f"search text too long ({len(text)})")
        if e.get("level_center") and not ((c.get("center") or {}).get("key") == "level"):
            F(f"level did not centre: center={c.get('center')} must.level={(c.get('must') or {}).get('level')}")
        if "level_center_value" in e and (c.get("center") or {}).get("value") != e["level_center_value"]:
            F(f"center {c.get('center')} != level {e['level_center_value']}")
        if e.get("no_company_constraint") and any("company" in (c.get(s) or {}) for s in ("must", "prefer", "avoid")):
            F("the intake constrained company")
        for v in e.get("musts_exclude_values") or []:
            if any(v in [str(x) for x in vals] for vals in (c.get("must") or {}).values() if isinstance(vals, list)):
                F(f"must carries {v!r}")
        for key in e.get("must_excludes_keys") or []:
            if key in (c.get("must") or {}):
                F(f"must still carries {key!r} ({c['must'][key]}) — the place-or-mode rule did not fire")
        for v in e.get("must_field_not") or []:
            if v in [str(x) for x in ((c.get("must") or {}).get("field") or [])]:
                F(f"field must is {v!r} — the co-occurrence correction did not fire")
        if "keeps_specific_constraint_any" in e:
            keys = set(c.get("must") or {}) | set(c.get("prefer") or {})
            if not any(k in keys for k in e["keeps_specific_constraint_any"]):
                F(f"no specific constraint survived among {e['keeps_specific_constraint_any']}: must={list((c.get('must') or {}).keys())} prefer={list((c.get('prefer') or {}).keys())}")
        for rule in e.get("notes_rules_include") or []:
            if not any(n.get("rule") == rule for n in ((self.ready or {}).get("notes") or [])):
                F(f"no {rule!r} note on the ready card ({[n.get('rule') for n in (self.ready or {}).get('notes') or []]})")
        if "skill_musts_max" in e and len((c.get("must") or {}).get("skill") or []) > e["skill_musts_max"]:
            F(f"{len(c['must']['skill'])} skill musts (> {e['skill_musts_max']})")
        if "musts_max_keys" in e and len(c.get("must") or {}) > e["musts_max_keys"]:
            F(f"{len(c.get('must') or {})} must keys after declining everything")
        for key, n in (e.get("must_lists_min") or {}).items():
            if len((c.get("must") or {}).get(key) or []) < n:
                F(f"must {key} has {len((c.get('must') or {}).get(key) or [])} values (< {n})")
        for key, n in (e.get("prefer_lists_min") or {}).items():
            if len((c.get("prefer") or {}).get(key) or []) < n:
                F(f"prefer {key} has {len((c.get('prefer') or {}).get(key) or [])} values (< {n})")
        if e.get("ready") and not self.ready:
            F("never READY")
        if "pool_max" in e and self.ready and (self.ready.get("pool") or 0) > e["pool_max"]:
            F(f"pool {self.ready.get('pool')} > {e['pool_max']}")
        if "diagnosis_top_key_in" in e:
            dg = (self.ready or {}).get("diagnosis") or {}
            top = ((dg.get("keys") or [{}])[0]).get("key")
            if top not in e["diagnosis_top_key_in"]:
                F(f"diagnosis top key {top!r} not in {e['diagnosis_top_key_in']} ({dg})")
        if e.get("no_context_question") and self.context_q:
            F("asked the draft context question although the opening carried the role")
        if e.get("context_question_asked") and not self.context_q:
            F("did not ask the draft context question on a thin opening")
        if e.get("context_reasked") and not getattr(self, "context_reasks", 0):
            F("a reply that named no role was not asked again")
        if e.get("no_draft") and self.draft_first:
            F(f"drafted from nothing: {self.draft_first.get('title')!r} peers {[p.get('title') for p in self.draft_first.get('peers') or []][:4]}")
        if "handoff_rows_min_strict" in e and len(self.handoff or []) < e["handoff_rows_min_strict"]:
            F(f"hand-off returned {len(self.handoff or [])} rows — a diagnosis is not an excuse for a hiring flow (must={(c.get('must'))})")
        if "secs_max" in e and getattr(self, "secs", 0) > e["secs_max"]:
            F(f"took {getattr(self, 'secs', 0)}s (> {e['secs_max']}s)")
        if e.get("which_jd_asked") and not self.which_jd:
            F("did not offer the saved JDs")
        d0, d1 = self.draft_first, self.draft_last
        if any(x in e for x in ("draft_title_words_any", "draft_has_you_said", "draft_all_lines_cited", "draft_peer_titles_words_any", "draft_sparse_or_relevant", "draft_title_excludes")) and not d0:
            F("no draft was produced")
        if d0:
            title = str(d0.get("title") or "").lower()
            if "draft_title_words_any" in e and not any(w in title for w in e["draft_title_words_any"]):
                F(f"draft title {title!r} names none of {e['draft_title_words_any']}")
            for w in e.get("draft_title_excludes") or []:
                if w in title:
                    F(f"draft title {title!r} is off-role")
            lines = [x for sec in ("responsibilities", "must_have", "nice_to_have") for x in (d0.get(sec) or [])]
            if e.get("draft_has_you_said") and not any((x.get("support") or {}).get("you") for x in lines):
                F("draft has no 'you said' line")
            if e.get("draft_all_lines_cited") and any(not ((x.get("support") or {}).get("you") or (x.get("support") or {}).get("count")) for x in lines):
                F("draft has an uncited line")
            if "draft_peer_titles_words_any" in e:
                bad = [p.get("title") for p in (d0.get("peers") or []) if not any(w in str(p.get("title") or "").lower() for w in e["draft_peer_titles_words_any"])]
                if len(bad) > max(2, len(d0.get("peers") or []) // 3):
                    F(f"peers off-role: {bad[:5]}")
            if e.get("draft_sparse_or_relevant") and not d0.get("sparse"):
                self.notes.append(f"thin opening produced a non-sparse draft: {[p.get('title') for p in d0.get('peers') or []][:5]}")
        if "redraft_contains" in e:
            t1 = str((d1 or {}).get("text") or "").lower()
            for w in e["redraft_contains"]:
                if w not in t1:
                    F(f"redraft lacks {w!r}")
        if "handoff_rows_min" in e:
            n = len(self.handoff or [])
            if self.ready and (self.ready.get("diagnosis") or {}).get("keys") and n == 0:
                self.notes.append("empty hand-off explained by a diagnosis")
            elif n < e["handoff_rows_min"]:
                F(f"hand-off returned {n} rows (< {e['handoff_rows_min']})")
        if "handoff_top_levels_include" in e and self.handoff:
            top = [((r.get("facets") or {}).get("level") or [None])[0] for r in self.handoff[:10]]
            if not any(l in e["handoff_top_levels_include"] for l in top):
                F(f"top-10 levels {top} include none of {e['handoff_top_levels_include']}")
        for w in e.get("handoff_top_titles_exclude") or []:
            if any(w.lower() in str(r.get("title") or "").lower() for r in (self.handoff or [])[:5]):
                F(f"top-5 hand-off titles include {w!r}")
        if self.improved is not None:
            imp = self.improved
            if "improve_kind" in e and imp.get("kind") != e["improve_kind"]:
                F(f"improve kind {imp.get('kind')!r}")
            t = str(imp.get("text") or "").lower()
            if "improve_mentions_any" in e and not any(w in t for w in e["improve_mentions_any"]):
                F("improved text carries none of the answers")
            if e.get("improve_added_verified") and any(a not in (imp.get("text") or "") for a in (imp.get("added") or [])):
                F("an 'added' line is not in the text")
            for w in e.get("improve_no_new_employer") or []:
                if w in t:
                    F(f"improved text invented {w!r}")
        return self.failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("ROSTER_EVAL_BASE", "https://roster-api-production-3405.up.railway.app"))
    ap.add_argument("--only", default="")
    ap.add_argument("--scenarios", default=os.path.join(HERE, "scenarios.jsonl"))
    args = ap.parse_args()
    base = args.base.rstrip("/")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    scenarios = [json.loads(l) for l in open(args.scenarios) if l.strip()]
    # a live posting URL for the link scenario
    live_url = ""
    try:
        d = post(base, "/search/evaluate", {"contract": {"kind": "job", "text": "backend engineer", "must": {"field": ["software"]}, "limit": 5}})
        live_url = next((r.get("url") for r in d.get("rows") or [] if str(r.get("url") or "").startswith("http")), "")
    except Exception:   # noqa: BLE001
        pass
    results = []; t_all = time.time()
    for sc in scenarios:
        if only and sc["id"] not in only:
            continue
        run = Run(base, sc, live_url); t0 = time.time()
        try:
            run.seed(); run.drive(); run.secs = round(time.time() - t0, 1); run.score()
        except Exception as e:   # noqa: BLE001
            run.failures.append(f"exception: {type(e).__name__}: {str(e)[:160]}")
        finally:
            run.cleanup()
        ok = not run.failures
        k = ((run.state or {}).get("kernel") or {})
        results.append({"id": sc["id"], "ok": ok, "failures": run.failures, "notes": run.notes, "asked": run.asked, "secs": round(time.time() - t0, 1),
                        "direction": k.get("direction"), "artifact": k.get("artifact"), "contract": ((run.ready or {}).get("contract")), "pool": (run.ready or {}).get("pool"),
                        "handoff_rows": len(run.handoff or []), "draft_title": ((run.draft_first or {}).get("title")), "draft_peers": [p.get("title") for p in ((run.draft_first or {}).get("peers") or [])][:6]})
        print(f"{'PASS' if ok else 'FAIL'}  {sc['id']:38s} {results[-1]['secs']:6.1f}s  asked={len(run.asked)} rows={results[-1]['handoff_rows']}" + (("  " + " | ".join(run.failures)) if run.failures else "") + (("  [" + "; ".join(run.notes) + "]") if run.notes else ""), flush=True)
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    path = os.path.join(HERE, "runs", time.strftime("intake-%Y%m%d-%H%M%S.json"))
    json.dump({"base": base, "secs": round(time.time() - t_all), "results": results}, open(path, "w"), indent=1)
    n_ok = sum(r["ok"] for r in results)
    print(f"\n{n_ok}/{len(results)} passed in {round(time.time() - t_all)}s → {os.path.relpath(path)}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
