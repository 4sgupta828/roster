"""Definition-driven application plans: ATS form definitions → plan → policy classes (no browser, no network)."""
from api.apply_adapters import detect, policy_for
from api.apply_adapters.ashby import definition_to_form as ashby_form
from api.apply_adapters.greenhouse import definition_to_form as gh_form, parse_url as gh_parse
from api.apply_adapters.lever import html_to_form as lever_form
from api.apply_plan import apply_drafts, bind_plan, draftable, summary

PROFILE = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "555", "linkedin": "https://linkedin.com/in/ada",
           "requires_sponsorship": "No", "us_authorized_to_work": "Yes", "ack_privacy": "Yes", "veteran_status": "Prefer not to say", "city": "Austin", "region": "TX"}


def test_greenhouse_definition_becomes_addressable_questions():
    assert gh_parse("https://job-boards.greenhouse.io/gusto/jobs/7948318") == ("gusto", "7948318")
    assert gh_parse("https://job-boards.greenhouse.io/embed/job_app?for=sofi&token=7833122003") == ("sofi", "7833122003")
    assert gh_parse("https://www.sofi.com/careers/job/?gh_jid=7833122003") == ("", "7833122003")
    d = {"title": "Engineer", "company_name": "Gusto",
         "questions": [{"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
                       {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
                       {"label": "Will you now or in the future require sponsorship?", "required": True,
                        "fields": [{"name": "question_1", "type": "multi_value_single_select", "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]}],
         "demographic_questions": {"questions": [{"id": 9, "label": "Veteran status", "required": False, "type": "multi_value_single_select",
                                                  "answer_options": [{"id": 1, "label": "I am not a protected veteran"}, {"id": 2, "label": "Prefer not to say"}]}]}}
    f = gh_form(d, board="gusto", job_id="7948318")
    ids = [q["id"] for q in f["questions"]]
    assert ids == ["first_name", "resume", "question_1", "demographic_question_9"]
    assert f["questions"][2]["kind"] == "select" and f["questions"][2]["options"] == ["Yes", "No"]
    assert f["questions"][3]["policy"] == "identity_sensitive" and f["questions"][1]["policy"] == "file"
    assert f["form_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=gusto&token=7948318"


def test_ashby_definition_keeps_boolean_questions_the_dom_never_showed():
    jp = {"title": "DS Engineer", "applicationForm": {"sections": [{"title": "", "fieldEntries": [
        {"isRequired": True, "field": {"path": "_systemfield_name", "title": "Full Legal Name", "type": "String"}},
        {"isRequired": True, "field": {"path": "_systemfield_resume", "title": "Resume", "type": "File"}},
        {"isRequired": True, "field": {"path": "abc-123", "title": "Are you legally authorized to work in the United States?", "type": "Boolean"}},
        {"isRequired": False, "field": {"path": "def-456", "title": "Gender", "type": "ValueSelect", "selectableValues": [{"label": "Male"}, {"label": "Decline to self-identify"}]}}]}]}}
    f = ashby_form(jp, org="d-matrix", job_id="x")
    kinds = {q["id"]: (q["kind"], q["policy"], q["options"]) for q in f["questions"]}
    assert kinds["abc-123"] == ("boolean", "open", ["Yes", "No"])
    assert kinds["def-456"][1] == "identity_sensitive"
    assert kinds["_systemfield_resume"] == ("file", "file", [])


def test_lever_page_parses_standard_and_custom_questions():
    page = '''<form><input name="name" required><input name="email"><input name="resume" type="file"><textarea name="comments"></textarea>
    <li class="application-question custom-question"><div class="application-label">Where are you located? ✱</div>
      <select name="cards[u1][field0]"><option>Select...</option><option>United States</option><option>Canada</option></select></li>
    <li class="application-question custom-question"><div class="application-label">Strategy?</div><textarea name="cards[u2][field0]" required></textarea></li></form>'''
    f = lever_form(page, site="acme", job_id="j")
    byid = {q["id"]: q for q in f["questions"]}
    assert byid["cards[u1][field0]"]["kind"] == "select" and byid["cards[u1][field0]"]["options"] == ["United States", "Canada"] and byid["cards[u1][field0]"]["required"]
    assert byid["cards[u2][field0]"]["kind"] == "textarea" and byid["cards[u2][field0]"]["required"]
    assert byid["name"]["required"] and byid["resume"]["policy"] == "file"


def test_bind_plan_sources_policies_and_blocking():
    f = {"ats": "greenhouse", "questions": [
        {"id": "first_name", "label": "First Name", "kind": "text", "options": [], "required": True, "policy": "open", "selector": "#first_name"},
        {"id": "q1", "label": "Will you require visa sponsorship?", "kind": "select", "options": ["Yes", "No"], "required": True, "policy": "open", "selector": ""},
        {"id": "q2", "label": "Why this role?", "kind": "textarea", "options": [], "required": True, "policy": "open", "selector": ""},
        {"id": "q3", "label": "Why do you want to work here?", "kind": "textarea", "options": [], "required": False, "policy": "open", "selector": ""},
        {"id": "d1", "label": "Veteran status", "kind": "select", "options": ["I am not a protected veteran", "Prefer not to say"], "required": False, "policy": "identity_sensitive", "selector": ""},
        {"id": "l1", "label": "I acknowledge the privacy notice", "kind": "checkbox", "options": ["I acknowledge the privacy notice"], "required": True, "policy": "legal", "selector": ""},
        {"id": "resume", "label": "Resume/CV", "kind": "file", "options": [], "required": True, "policy": "file", "selector": ""}]}
    p = bind_plan(f, PROFILE, {"Why do you want to work here?": "Because of the mission."}, {})
    by = {x["id"]: x for x in p["plan"]}
    assert by["first_name"]["answer"] == "Ada" and by["first_name"]["source"] == "profile"
    assert by["q1"]["answer"] == "No" and by["q1"]["source"] == "profile"
    assert by["q3"]["answer"] == "Because of the mission." and by["q3"]["source"] == "saved answer"
    assert by["d1"]["answer"] == "Prefer not to say" and by["l1"]["answer"].startswith("I acknowledge")
    assert by["q2"]["blocking"] and summary(p)["blocking"] == ["Why this role?"]
    assert [d["id"] for d in draftable(p)] == ["q2"]                       # identity / legal never draftable
    apply_drafts(p, {"Why this role?": "I built exactly this.", "Veteran status": "I am not a protected veteran"})
    assert by["q2"]["answer"] == "I built exactly this." and by["q2"]["source"] == "agent draft" and not by["q2"]["blocking"]
    assert by["d1"]["answer"] == "Prefer not to say"                       # a draft can never touch an identity field
    p2 = bind_plan(f, PROFILE, {}, {"Will you require visa sponsorship?": "Yes"})
    assert {x["id"]: x for x in p2["plan"]}["q1"]["source"] == "your answer"


def test_policy_and_detection():
    assert policy_for("What is your race?", "select") == "identity_sensitive"
    assert policy_for("I certify the above is true", "checkbox") == "legal"
    assert policy_for("Resume", "file") == "file" and policy_for("Why us?", "textarea") == "open"
    assert detect("https://acme.wd5.myworkdayjobs.com/x") == "workday" and detect("https://careers.acme.com/jobs/1") == ""
