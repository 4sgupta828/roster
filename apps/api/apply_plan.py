"""THE APPLICATION PLAN — the contract between the planner (server), the review (UI) and the executor
(the Chrome extension in the user's own browser). Built from the ATS's OWN form definition
(apply_adapters), bound in code to: the Apply profile → the answer bank → constrained drafts.

Policy classes are structural, not rules of thumb:
  identity_sensitive  filled only from explicit profile defaults (EEO); never drafted
  legal               filled only from the profile's standing pre-approval; never drafted
  file                the résumé — attached by the extension from Roster's copy
  never               CAPTCHA / passwords — not in any plan
Required questions without an answer BLOCK execution until the user answers.
"""
from __future__ import annotations

from api.auto_apply import _pick_option, map_label, norm_question, standard_answer, value_for

_TEXT_KINDS = ("text", "textarea", "email", "tel", "url", "date")
_CHOICE_KINDS = ("select", "multiselect", "radio", "checkbox", "boolean")


def bind_plan(form: dict, profile: dict, bank: dict | None = None, user_answers: dict | None = None) -> dict:
    """form (apply_adapters.FormDef) → plan: every question with {answer, source}. Sources:
    profile | saved answer | your answer | '' (open). Drafting for open ones is the caller's step
    (draft_plan) — it never touches identity_sensitive / legal / file / never questions."""
    bank_n = {norm_question(k): v for k, v in (bank or {}).items()}
    ua = {norm_question(k): v for k, v in (user_answers or {}).items()}
    plan = []
    for q in form.get("questions") or []:
        kind, label, opts, pol = q["kind"], q["label"], q.get("options") or [], q.get("policy") or "open"
        ans, src = "", ""
        if pol == "never":
            plan.append({**q, "answer": "", "source": "never", "blocking": False})
            continue
        if pol == "file":
            plan.append({**q, "answer": "<résumé>" if "resume" in q["id"].lower() or "résumé" in label.lower() or "cv" in label.lower() else "",
                         "source": "profile" if "resume" in q["id"].lower() or "cv" in label.lower() else "", "blocking": False})
            continue
        if q["id"].endswith("_text") and kind == "textarea" and any(w in label.lower() for w in ("resume", "résumé", "cv", "cover letter")):
            plan.append({**q, "answer": "", "source": "alternative to the file", "blocking": False, "policy": "skip"})
            continue
        u = ua.get(norm_question(label)) or ua.get(norm_question(q["id"]))
        if u:
            ans, src = str(u), "your answer"
        elif pol in ("identity_sensitive", "legal"):
            ans = standard_answer(label, kind, opts, profile)
            src = "profile" if ans else ""
            if not ans and pol == "identity_sensitive" and opts:
                # no stated default: the DECLINE option is the only answer we may choose for the user (reviewable)
                dec = _pick_option(opts, "prefer not to say")
                if dec:
                    ans, src = dec, "declined by default"
        else:
            key = map_label(label) if kind in _TEXT_KINDS else ""
            v = value_for(key, profile) if key else ""
            if v:
                ans, src = v, "profile"
            else:
                b = bank_n.get(norm_question(label))
                if b:
                    ans, src = str(b), "saved answer"
                else:
                    sa = standard_answer(label, kind, opts, profile)
                    if not sa and opts and kind in _CHOICE_KINDS and "locat" in label.lower():
                        sa = _pick_option(opts, str(profile.get("country") or "")) or _pick_option(opts, str(profile.get("region") or ""))
                    if sa:
                        ans, src = sa, "profile"
        if ans and kind in _CHOICE_KINDS and opts and ans not in opts:
            picked = _pick_option(opts, ans)
            ans = picked or ""
            if not ans:
                src = ""
        plan.append({**q, "answer": ans, "source": src, "blocking": bool(q.get("required")) and not ans})
    return {**form, "plan": plan}


def draftable(plan: dict) -> list[dict]:
    """The open questions a model MAY draft: policy 'open' only, no answer yet."""
    return [p for p in plan.get("plan") or [] if not p.get("answer") and (p.get("policy") or "open") == "open" and p["kind"] != "file"]


def apply_drafts(plan: dict, drafts: dict) -> dict:
    """Fill open questions with drafts (choice answers validated against the real options)."""
    for p in plan.get("plan") or []:
        if p.get("answer") or (p.get("policy") or "open") != "open":
            continue
        d = (drafts or {}).get(p["label"]) or (drafts or {}).get(p["id"])
        if not d:
            continue
        if p["kind"] in _CHOICE_KINDS and p.get("options"):
            d = _pick_option(p["options"], str(d))
            if not d:
                continue
        p["answer"], p["source"], p["blocking"] = str(d), "agent draft", False
    return plan


def summary(plan: dict) -> dict:
    ps = plan.get("plan") or []
    return {"total": len(ps), "answered": sum(1 for p in ps if p.get("answer")),
            "blocking": [p["label"] for p in ps if p.get("blocking")],
            "by_source": {s: sum(1 for p in ps if p.get("source") == s) for s in ("profile", "saved answer", "agent draft", "your answer")}}
