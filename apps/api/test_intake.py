"""Guided intake service (docs/specs/guided-intake.md §3): stage transitions with a FAKE model and the kernel's
in-memory facet store — no DB, no spend. The model's job is only the words; every gate is code's."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import Contract, InMemoryFacetStore
from roster_vertical.facet_schema import FACET_SCHEMA

from api.intake import IntakeService

JOBS = [
    {"id": f"j{i}", "kind": "job", "sim": 0.6, "title": "t", "facets": {"field": ["software"], "level": ["senior" if i % 2 else "staff_plus"], "metro": ["austin"] if i % 3 else ["bay_area"],
                                                                     "work_mode": ["remote"] if i % 2 else ["onsite"], "company_type": ["startup"] if i % 2 else ["public"], "country": ["us"]}}
    for i in range(20)
]
PEOPLE = [
    {"id": f"p{i}", "kind": "person", "sim": 0.6, "name": f"P{i}", "facets": {"field": ["software"], "level": ["senior" if i % 2 else "leadership"], "metro": ["bay_area"],
                                                                         "work_type": ["ic"] if i % 2 else ["manager"], "evidence": ["repos"] if i % 4 else [], "country": ["us"]}}
    for i in range(20)
]


def _run(coro):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class FakeLLM:
    """Answers by prompt kind: the completeness read reports what a résumé / JD states; the turn read maps a reply."""
    def __init__(self):
        self.calls = []

    def __call__(self, system: str, user: str) -> dict:
        self.calls.append((system[:40], user[:80]))
        if "report which of these items it states" in system:
            if "Software engineer, Acme" in user:      # a terse résumé
                return {"present": {"current_role": "software engineer at Acme", "years": "5", "field": "software", "skills": "python, go"},
                        "missing": ["level", "location", "comp"], "weak": ["skills"]}
            if "Backend Engineer, Payments" in user:  # a short JD
                return {"present": {"title": "Backend Engineer, Payments", "responsibilities": "own ledger services"},
                        "missing": ["level", "location", "comp"], "weak": ["must_skills"]}
            return {"present": {}, "missing": ["level"], "weak": []}
        if '{"direction": "looking" | "hiring" | null}' in system:
            r = user.lower()
            return {"direction": "looking" if ("looking for" in r or "my next role" in r or "find me roles" in r) else "hiring" if ("hire" in r or "hiring" in r) else None}
        if "The user was asked ONE question" in system:
            reply = user.split("REPLY:", 1)[-1].strip().lower()
            if "prefer not" in reply:
                return {"answers": {"comp": None}, "free_text": "", "direction": None}
            if "staff" in reply:
                return {"answers": {"level": "staff_plus"}, "free_text": "", "direction": None}
            if "remote" in reply:
                return {"answers": {"location": "remote", "work_mode": "remote"}, "free_text": "", "direction": None}
            if "looking for" in reply:
                return {"answers": {}, "free_text": reply, "direction": "job"}
            if "hiring" in reply:
                return {"answers": {}, "free_text": reply, "direction": "candidate"}
            return {"answers": {}, "free_text": reply, "direction": None}
        raise AssertionError("unexpected prompt")


def _service(llm=None, profile=None):
    store = InMemoryFacetStore(JOBS + PEOPLE, FACET_SCHEMA)

    async def counts_fn(kind, must):
        return await store.counts(kind, must, FACET_SCHEMA)

    def compile_fn(kind, text, *, limit=60, scope=None):
        # the real compile is a model call; here: a contract from the words
        must = {"field": ["software"]} if "software" in text.lower() or "engineer" in text.lower() else {}
        return Contract(kind=kind, text=text[:200], must=must, limit=limit, scope=dict(scope or {}))

    async def profile_fn(user):
        return profile

    return IntakeService(schema=FACET_SCHEMA, llm_json=(llm or FakeLLM()), counts_fn=counts_fn, compile_fn=compile_fn, profile_fn=profile_fn)


def test_direction_is_asked_when_the_words_do_not_say():
    s = _service()
    out = _run(s.step(message="hi", state=None))
    assert out["stage"] == "direction" and out["question"]["kind"] == "direction"
    assert [o[0] for o in out["question"]["options"]] == ["job", "candidate"]


def test_job_seeker_with_a_terse_self_description_walks_gaps_then_keys_then_ready():
    llm = FakeLLM(); s = _service(llm)
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    assert out["stage"] == "artifact" and out["question"]["kind"] == "artifact" and out["question"]["name"] == "profile"
    # a self-description is the artifact
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    assert out["state"]["kernel"]["artifact"]["status"] == "present"
    assert out["state"]["kernel"]["checklist"]["years"] == "present" and out["state"]["kernel"]["checklist"]["level"] == "missing"
    assert out["stage"] == "questions" and out["question"]["kind"] == "item" and out["question"]["name"] == "level"
    assert out["question"]["words"].endswith("?") and any(o[0] == "senior" for o in out["question"]["options"])   # options from the counts
    assert out["question"]["options"][0][1]                                                                # labeled
    # a typed reply is read by the model; 'staff' → level staff_plus → the contract PREFERS it (a profile level ranks)
    out = _run(s.step(message="staff, more or less", state=out["state"]))
    assert out["state"]["kernel"]["contract"]["prefer"]["level"] == ["staff_plus"]
    assert out["question"]["name"] == "location"
    # a chip tap answers without the model
    out = _run(s.step(answer={"name": "location", "value": "austin"}, state=out["state"]))
    assert out["state"]["kernel"]["contract"]["must"]["metro"] == ["austin"]
    assert out["question"]["name"] == "comp"
    out = _run(s.step(message="prefer not to say", state=out["state"]))
    assert out["state"]["kernel"]["checklist"]["comp"] == "skipped" and "comp" not in out["state"]["kernel"]["contract"]["must"]
    # gaps done (skills was weak: never asked); required keys field / level / metro are constrained → optional
    # keys in the vertical's order: company_type and work_mode both split the pool evenly → asked (budget 2); then ready
    assert out["question"]["kind"] == "key" and out["question"]["name"] == "company_type" and out["question"]["klass"] == "optional"
    out = _run(s.step(answer={"name": "company_type", "value": "startup"}, state=out["state"]))
    assert out["state"]["kernel"]["contract"]["prefer"]["company_type"] == ["startup"]       # an optional answer only ranks
    assert out["question"]["name"] == "work_mode"
    out = _run(s.step(answer={"name": "work_mode", "value": "remote"}, state=out["state"]))
    assert out["stage"] == "ready"
    r = out["ready"]
    assert r["contract"]["kind"] == "job" and r["pool"] > 0 and r["understood"].endswith(".") and r["artifact"]["kind"] == "profile"
    assert r["advice"] and isinstance(r["advice"], list)
    assert r["transcript_audit"] and r["transcript_audit"][-1]["role"] == "assistant"


def test_resume_on_file_makes_a_job_seeker_ready_fast_and_a_chip_answer_needs_no_model():
    llm = FakeLLM()
    s = _service(llm, profile={"_resume_text": "Software engineer, Acme, 2019 to now. Python, Go.", "current_title": "Software engineer"})
    out = _run(s.step(message="find me roles", state=None, direction="job", user={"id": "u1"}))
    assert out["state"]["kernel"]["artifact"]["status"] == "present" and out["state"]["kernel"]["artifact"]["source"] == "on_file"
    assert out["stage"] == "questions"
    n_model = len(llm.calls)
    out = _run(s.step(answer={"name": out["question"]["name"], "value": out["question"]["options"][0][0]}, state=out["state"]))
    assert len(llm.calls) == n_model                                                       # a tap costs nothing


def test_hiring_manager_pastes_a_short_jd_and_gaps_are_asked():
    s = _service()
    out = _run(s.step(message="I'm hiring", state=None))
    assert out["state"]["kernel"]["direction"] == "candidate" and out["question"]["name"] == "jd"
    jd = "Backend Engineer, Payments. " + "You will own our ledger services and money movement. " * 5
    out = _run(s.step(message=jd, state=out["state"]))
    k = out["state"]["kernel"]
    assert k["artifact"]["status"] == "present" and k["checklist"]["title"] == "present" and k["checklist"]["level"] == "missing"
    assert out["question"]["kind"] == "item" and out["question"]["name"] == "level"
    assert out["state"]["kernel"]["contract"]["kind"] == "person"


def test_search_now_forces_ready_with_what_is_known_and_a_model_error_never_dead_ends():
    class Boom(FakeLLM):
        def __call__(self, system, user):
            if "The user was asked ONE question" in system:
                raise RuntimeError("model down")
            return super().__call__(system, user)
    s = _service(Boom())
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    assert out["stage"] == "questions"
    out2 = _run(s.step(message="staff", state=out["state"]))                                   # the turn read fails
    assert out2["stage"] == "ready" and out2["ready"]["note"]                                  # fail-safe READY, with a note
    out3 = _run(s.step(search_now=True, state=out["state"]))
    assert out3["stage"] == "ready" and out3["ready"]["contract"]["must"]["field"] == ["software"]


def test_the_transcript_is_capped_and_never_holds_the_artifact_text():
    s = _service()
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go." + " x" * 4000, state=out["state"]))
    assert all(len(m["text"]) <= 2000 for m in out["state"]["transcript"])
    assert len(out["state"]["artifact_text"]) <= 6000


def test_intake_endpoint_runs_a_turn_and_the_people_surface_accepts_a_contract(monkeypatch):
    """The HTTP surface: flag-gated; a fresh turn asks the direction; a READY contract sent to /search on the
    People surface runs the evaluator (card-shaped rows + the rail) instead of the old engine."""
    from fastapi.testclient import TestClient
    from api.app import create_app
    monkeypatch.setenv("ROSTER_GUIDED_INTAKE", "1")
    monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(JOBS + PEOPLE, FACET_SCHEMA)
    app.state.intake_llm = FakeLLM()
    c = TestClient(app)
    r = c.post("/intake/step", json={"message": "hi"})
    assert r.status_code == 200 and r.json()["question"]["kind"] == "direction"
    r2 = c.post("/intake/step", json={"message": "I'm hiring", "state": r.json()["state"]})
    assert r2.json()["question"]["name"] == "jd"
    r3 = c.post("/research", json={"question": "senior engineers", "tenant_id": "demo", "surface": "people",
                                 "contract": {"kind": "person", "text": "senior engineers", "must": {"level": ["senior"]}}})
    assert r3.status_code == 200, r3.text
    d = r3.json()
    assert d["people_rows"] and all(p["facets"]["level"] == ["senior"] for p in d["people_rows"])
    assert d["facet_nav"]["counts"]["level"] == {"senior": 10} and d["facet_nav"]["contract"]["must"] == {"level": ["senior"]}


def test_intake_endpoint_is_flag_gated(monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import create_app
    monkeypatch.delenv("ROSTER_GUIDED_INTAKE", raising=False)
    assert TestClient(create_app()).post("/intake/step", json={"message": "hi"}).status_code == 404


def test_a_reply_that_does_not_answer_the_question_never_repeats_it_and_volunteered_facts_land():
    """Prod 2026-09-05: 'senior, remote, around 200k' to 'how many years?' was asked six times. The model's reply
    shape (a literal 'name' key, no entry for the asked item) must mark the item skipped and keep the volunteered
    level / work mode."""
    class Volunteer(FakeLLM):
        def __call__(self, system, user):
            if "The user was asked ONE question" in system:
                return {"answers": {"name": None, "level": "senior", "work_mode": "remote"}, "free_text": "around 200k", "direction": "candidate"}
            return super().__call__(system, user)
    s = _service(Volunteer())
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    asked = [out["question"]["name"]]
    for _ in range(6):
        if out["stage"] == "ready":
            break
        out = _run(s.step(message="senior, remote, around 200k", state=out["state"]))
        if out["stage"] != "ready":
            asked.append(out["question"]["name"])
    assert len(asked) == len(set(asked)), asked                                            # never the same question twice
    k = out["state"]["kernel"]
    assert k["contract"]["prefer"].get("level") == ["senior"] and k["contract"]["must"].get("work_mode") == ["remote"]
    assert k["direction"] == "job"                                                          # a mid-intake 'direction' never flips it


def test_direction_reads_are_unambiguous(monkeypatch):
    """'I'm looking for my next role' is a job seeker; 'I need to hire a backend engineer' is a hiring manager."""
    class Dir(FakeLLM):
        def __call__(self, system, user):
            return super().__call__(system, user)
    s = _service(Dir())
    assert _run(s.step(message="I'm looking for my next role", state=None))["state"]["kernel"]["direction"] == "job"
    assert _run(s.step(message="I need to hire a backend engineer", state=None))["state"]["kernel"]["direction"] == "candidate"
    assert _run(s.step(message="hello", state=None))["question"]["kind"] == "direction"


def test_multi_select_answers_land_as_lists_on_items_and_keys():
    s = _service()
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    assert out["question"]["name"] == "level"
    out = _run(s.step(answer={"name": "level", "value": ["senior", "staff_plus", "not-a-level"]}, state=out["state"]))
    assert sorted(out["state"]["kernel"]["contract"]["prefer"]["level"]) == ["senior", "staff_plus"]          # illegal token dropped
    # a key question with several chips
    while out["stage"] != "ready" and out["question"]["kind"] != "key":
        out = _run(s.step(answer={"name": out["question"]["name"], "value": "__decline__"}, state=out["state"]))
    if out["stage"] != "ready":
        name = out["question"]["name"]
        vals = [o[0] for o in out["question"]["options"]][:2]
        out = _run(s.step(answer={"name": name, "value": vals}, state=out["state"]))
        k = out["state"]["kernel"]["contract"]
        assert sorted((k["must"].get(name) or k["prefer"].get(name) or [])) == sorted(vals)


def test_saved_jds_are_offered_and_a_pick_becomes_the_artifact():
    async def briefs_fn(user):
        return [{"id": 7, "kind": "jd", "title": "Senior Payments Backend", "role_key": "software/engineering/senior/payments", "text": "Backend Engineer, Payments. " * 12},
                {"id": 9, "kind": "jd", "title": "ML Platform Lead", "role_key": "data_ml/engineering/leadership/ml-platform-lead", "text": "ML Platform Lead. " * 20}]
    s = _service(); s.briefs_fn = briefs_fn
    out = _run(s.step(message="I'm hiring again", state=None, direction="candidate", user={"id": "u1"}))
    q = out["question"]
    assert q["kind"] == "artifact" and q["name"] == "jd" and [o[0] for o in q["options"]][:2] == ["brief:7", "brief:9"] and "new" in [o[0] for o in q["options"]]
    out = _run(s.step(answer={"name": "jd", "value": "brief:7"}, state=out["state"], user={"id": "u1"}))
    k = out["state"]["kernel"]
    assert k["artifact"]["status"] == "present" and k["artifact"]["source"] == "saved" and k["artifact"]["brief_id"] == 7
    assert out["stage"] == "questions"


def test_draft_path_builds_from_context_takes_changes_and_accepts():
    calls = []
    async def draft_fn(role_text, context):
        calls.append(("draft", role_text)); return {"title": "Senior Backend Engineer, Payments", "text": "Senior Backend Engineer, Payments\n\nMust have\n- Built ledgering systems\n", "must_have": [{"text": "Built ledgering systems", "support": {"you": True}}], "peers": [{"id": 1}] * 10, "centre": [], "optional": [], "market": {}, "sparse": False}
    async def redraft_fn(draft, change):
        calls.append(("redraft", change)); d = dict(draft); d["text"] = draft["text"] + "- Kafka\n"; return d
    s = _service(); s.draft_fn = draft_fn; s.redraft_fn = redraft_fn
    out = _run(s.step(message="I'm hiring", state=None))
    out = _run(s.step(answer={"name": "jd", "value": "draft"}, state=out["state"]))
    assert out["question"]["kind"] == "draft_context"
    out = _run(s.step(message="senior backend engineer for our payments team, SF or remote, must have built ledgering", state=out["state"]))
    assert out["question"]["kind"] == "draft" and out["question"]["draft"]["title"].startswith("Senior Backend") and calls[0][0] == "draft"
    out = _run(s.step(message="add Kafka as a must", state=out["state"]))
    assert calls[-1] == ("redraft", "add Kafka as a must") and "- Kafka" in out["question"]["draft"]["text"]
    out = _run(s.step(answer={"name": "jd", "value": "accept"}, state=out["state"]))
    k = out["state"]["kernel"]
    assert k["artifact"]["status"] == "present" and k["artifact"]["source"] == "drafted" and out["state"]["artifact_text"].startswith("Senior Backend")
    assert out["stage"] in ("questions", "ready")


def test_improve_uses_only_the_original_and_the_answers():
    class Improver(FakeLLM):
        def __call__(self, system, user):
            if "fuller" in system:
                assert "ORIGINAL" in user and "ANSWERS" in user
                return {"text": "Software engineer at Acme (2019–now)\nLevel: senior\nSkills: Python, Go\nPrefers remote roles", "added": ["Level: senior", "Prefers remote roles", "Has a PhD from MIT"]}
            return super().__call__(system, user)
    s = _service(Improver())
    out = _run(s.step(message="I'm looking for my next role", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    out = _run(s.step(answer={"name": "level", "value": "senior"}, state=out["state"]))
    imp = _run(s.improve(state=out["state"]))
    assert imp["kind"] == "profile" and "Level: senior" in imp["text"]
    assert imp["added"] == ["Level: senior", "Prefers remote roles"]                  # a claimed addition absent from the text is dropped


def test_the_search_text_is_the_conversations_intent_not_the_artifact_and_level_centres():
    """Prod 2026-09-05: the Jobs hand-off ran with the whole résumé as the semantic text → results all over the
    place. The contract's text must be the opening words + the answers (short); the artifact only feeds the
    checklist; the level answer centres the ranking instead of filtering."""
    resume = "Sam Doe\nSan Francisco\nHead of ML Infrastructure at Acme (2019–now). " + "Built training platforms, feature stores, Kubernetes, Ray. " * 40
    s = _service(profile={"_resume_text": resume})
    out = _run(s.step(message="looking for a hands-on engineering leadership role in ML infra", state=None, direction="job", user={"id": "u1"}))
    k = out["state"]["kernel"]
    assert len(k["contract"]["text"]) <= 400 and "feature stores" not in k["contract"]["text"]
    assert "engineering leadership role in ML infra" in k["contract"]["text"]
    # walk to ready answering level with 'leadership'
    steps = 0
    while out["stage"] != "ready" and steps < 8:
        steps += 1
        q = out["question"]
        val = "leadership" if q["name"] == "level" else (q["options"][0][0] if q.get("options") else "__decline__")
        out = _run(s.step(answer={"name": q["name"], "value": val}, state=out["state"]))
    rd = out["ready"]; c = rd["contract"]
    assert c["center"] == {"key": "level", "value": "leadership", "span": 1} and "level" not in c["must"]
    assert len(c["text"]) <= 400 and "engineering leadership role in ML infra" in c["text"] and "feature stores" not in c["text"]
    assert "centred on leadership" in rd["understood"]


def test_the_current_title_never_becomes_the_search_when_the_user_said_what_they_want():
    resume = "Sam Doe. Founder in Residence at Studio X (2024–now). Before: Head of ML Infrastructure. Kubernetes, Ray, feature stores."
    s = _service(profile={"_resume_text": resume})
    out = _run(s.step(message="looking for a hands-on engineering leadership role in ML infra", state=None, direction="job", user={"id": "u1"}))
    out = _run(s.step(search_now=True, state=out["state"]))
    text = out["ready"]["contract"]["text"].lower()
    assert "founder in residence" not in text and "ml infra" in text


def test_chip_answers_appear_in_the_transcript_in_conversation_order():
    s = _service()
    out = _run(s.step(message="", state=None, direction="job"))
    out = _run(s.step(message="Software engineer, Acme, 2019 to now. Python, Go.", state=out["state"]))
    out = _run(s.step(answer={"name": "level", "value": "senior"}, state=out["state"]))
    roles = [(m["role"], m["text"][:30]) for m in out["state"]["transcript"]]
    assert roles[0] == ("user", "I'm looking for a role")
    assert ("user", "senior") in roles
    i = roles.index(("user", "senior"))
    assert roles[i - 1][0] == "assistant" and roles[i + 1][0] == "assistant"      # question · answer · next question


def test_an_empty_ready_names_the_must_to_drop():
    s = _service()
    out = _run(s.step(message="I'm hiring", state=None))
    jd = "Backend Engineer, Payments. " + "You will own our ledger services and money movement. " * 5
    out = _run(s.step(message=jd, state=out["state"]))
    # force an impossible combination through answers: level leadership AND metro 'nowhere'
    out = _run(s.step(answer={"name": out["question"]["name"], "value": "leadership" if out["question"]["name"] == "level" else out["question"]["options"][0][0]}, state=out["state"]))
    st = out["state"]; st["kernel"]["contract"]["must"]["metro"] = ["nowhere"]
    out = _run(s.step(search_now=True, state=st))
    rd = out["ready"]
    assert rd["pool"] == 0 and rd["diagnosis"] and rd["diagnosis"]["keys"][0]["key"] == "metro"
    assert rd["diagnosis"]["keys"][0]["without"] > 0


def test_a_rich_opening_drafts_without_the_context_question_and_a_reply_never_replaces_it():
    seen = []
    async def draft_fn(role_text, context):
        seen.append(role_text); return {"title": "CTO", "text": "CTO\n\nMust have\n- led a team\n", "must_have": [], "peers": [], "centre": [], "optional": [], "market": {}, "sparse": True}
    s = _service(); s.draft_fn = draft_fn
    opening = "I am looking to hire CTO for my company (startup) to lead 10-15 people engineering team. Distributed cloud systems, ML infra, streaming."
    out = _run(s.step(message=opening, state=None))
    assert out["state"]["kernel"]["direction"] == "candidate"
    out = _run(s.step(answer={"name": "jd", "value": "draft"}, state=out["state"]))
    assert out["question"]["kind"] == "draft" and seen and "hire CTO" in seen[0]          # no context question: the opening carries the role
    # a thin opening still asks, and the reply is COMBINED with the opening
    seen.clear(); s2 = _service(); s2.draft_fn = draft_fn
    out = _run(s2.step(message="I'm hiring", state=None))
    out = _run(s2.step(answer={"name": "jd", "value": "draft"}, state=out["state"]))
    assert out["question"]["kind"] == "draft_context"
    out = _run(s2.step(message="Keep going", state=out["state"]))
    assert out["question"]["kind"] == "draft" and seen and seen[0].startswith("I'm hiring")
