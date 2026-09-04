"""Controlled auto-apply: the CODE-OWNED parts (ATS detection, label → profile-field mapping, the fill
plan) — no browser here."""
from api.auto_apply import detect_ats, map_label, plan_fill, value_for


def test_ats_detection():
    assert detect_ats("https://boards.greenhouse.io/stripe/jobs/123") == "greenhouse"
    assert detect_ats("https://pinterestcareers.com/jobs/?gh_jid=55") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/offchainlabs/abc") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/level/xyz") == "ashby"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1") == "workday"
    assert detect_ats("https://careers.example.com/apply") == "generic"


def test_labels_map_to_profile_fields_and_custom_questions_stay_open():
    assert map_label("First Name *") == "first_name" and map_label("Last name") == "last_name"
    assert map_label("Email") == "email" and map_label("Phone number") == "phone"
    assert map_label("LinkedIn Profile") == "linkedin" and map_label("Resume/CV") == "resume"
    assert map_label("Current company") == "current_company"
    assert map_label("Are you authorized to work in the United States?") == ""
    assert map_label("Why do you want to work here?") == ""


def test_plan_fill_uses_profile_then_saved_answers_and_flags_required_open_questions():
    profile = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "555", "linkedin": "https://linkedin.com/in/ada",
               "city": "Austin", "region": "TX"}
    fields = [{"label": "First Name", "kind": "text", "name": "first_name", "required": True},
              {"label": "Last Name", "kind": "text", "name": "last_name", "required": True},
              {"label": "Email", "kind": "email", "name": "email", "required": True},
              {"label": "Resume/CV", "kind": "file", "name": "resume", "required": True},
              {"label": "Location (City)", "kind": "text", "name": "loc", "required": False},
              {"label": "Are you authorized to work in the US?", "kind": "select", "name": "q1", "required": True, "options": ["Yes", "No"]},
              {"label": "Why this role?", "kind": "textarea", "name": "q2", "required": False}]
    plan = plan_fill(fields, profile, {})
    keys = {f["key"]: f["value"] for f in plan["filled"]}
    assert keys["first_name"] == "Ada" and keys["email"] == "ada@example.com" and keys["resume"] == "<résumé file>"
    assert keys["city"] == "Austin, TX"
    assert [q["label"] for q in plan["open"]] == ["Are you authorized to work in the US?", "Why this role?"]
    assert [q["label"] for q in plan["blocking"]] == ["Are you authorized to work in the US?"]   # required + unanswered blocks submit
    plan2 = plan_fill(fields, profile, {"Are you authorized to work in the US?": "Yes"})
    assert not plan2["blocking"] and any(f["key"] == "answer" and f["value"] == "Yes" for f in plan2["filled"])
    assert value_for("full_name", profile) == "Ada Lovelace" and value_for("github", profile) == ""


def test_options_group_into_one_question_and_voluntary_ones_are_flagged():
    from api.auto_apply import group_fields
    fields = [{"label": "Yes, I will require sponsorship", "kind": "radio", "name": "visa", "group_key": "g1", "group_label": "Will you require visa sponsorship? *", "selector": "#v1"},
              {"label": "No, I will not", "kind": "radio", "name": "visa", "group_key": "g1", "group_label": "Will you require visa sponsorship? *", "selector": "#v2"},
              {"label": "East Asian", "kind": "checkbox", "name": "c1", "group_key": "g2", "group_label": "What are your racial, ethnic, and origin identities?", "selector": "#c1"},
              {"label": "White", "kind": "checkbox", "name": "c2", "group_key": "g2", "group_label": "What are your racial, ethnic, and origin identities?", "selector": "#c2"},
              {"label": "I certify the above is true", "kind": "checkbox", "name": "ack", "group_key": "g3", "group_label": "Overview Application", "selector": "#a1"},
              {"label": "Start typing...", "kind": "text", "name": "loc", "group_label": "Intended work location (city, state)", "selector": "#loc"}]
    g = group_fields(fields)
    labels = [(x["label"], x["kind"], len(x.get("options") or [])) for x in g]
    assert labels[0] == ("Will you require visa sponsorship?", "radio", 2)
    assert labels[1] == ("What are your racial, ethnic, and origin identities?", "checkbox", 2)
    assert labels[2] == ("I certify the above is true", "checkbox", 1)          # page chrome never names a question
    assert labels[3] == ("Intended work location (city, state)", "text", 0)   # a placeholder is not a label
    plan = plan_fill(fields, {"first_name": "A"}, {})
    vol = [q for q in plan["open"] if q.get("voluntary")]
    assert [q["label"] for q in vol] == ["What are your racial, ethnic, and origin identities?"]
    assert any(f["key"] == "answer" for f in plan_fill(fields, {}, {"Will you require visa sponsorship?": "No, I will not"})["filled"])
