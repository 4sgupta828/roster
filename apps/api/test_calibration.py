"""recruiter-workflows P2b: feedback tags → CODE-OWNED contract edits; the diff between two row snapshots."""
from api.calibration import diff_rows, diff_text, feedback_to_contract


def _row(eid, name, **attrs):
    return {"entity_id": eid, "name": name,
            "attributes": [{"key": k, "display": v} for k, v in attrs.items()],
            "evidence": {"strength": attrs.pop("_strength", "") if "_strength" in attrs else ""}}


ROWS = {
    "p1": _row("p1", "Ada", role="ML engineer", function="engineering", skill="pytorch", seniority="Senior", company="Stripe", metro="SF Bay Area"),
    "p2": _row("p2", "Bob", role="Data scientist", function="analytics", skill="sql", seniority="Junior", company="Acme", metro="Austin"),
    "p3": _row("p3", "Cy", role="ML engineer", function="engineering", skill="jax", seniority="Staff", company="Stripe", metro="NYC"),
}


def test_more_like_this_becomes_a_preference_not_a_filter():
    c = feedback_to_contract("ML engineers at Stripe", {"company": ["stripe"], "role": ["ml_engineer"]}, [],
                             [{"entity_id": "p1", "tags": ["more_like_this"], "state": "shortlist", "reviewer_name": "HM"}], ROWS)
    assert c["question"].startswith("ML engineers at Stripe — prefer ")
    assert "ML engineer" in c["question"] and "pytorch" in c["question"]
    assert c["refine_facets"] == {"company": ["stripe"], "role": ["ml_engineer"]}   # filters untouched
    assert c["exclude_ids"] == [] and c["edits"] and "HM wants more like Ada" in c["edits"][0]


def test_less_like_this_excludes_the_row_and_demotes_its_values():
    c = feedback_to_contract("ML engineers", {}, [], [{"entity_id": "p2", "tags": ["less_like_this"], "state": "not relevant"}], ROWS)
    assert c["exclude_ids"] == ["p2"]
    assert "analytics" in c["avoid_terms"] and "sql" in c["avoid_terms"]
    assert "excluded" in c["edits"][0]


def test_wrong_seniority_stops_gating_and_demotes_the_level():
    c = feedback_to_contract("senior ML engineers", {"role": ["ml_engineer"], "seniority": ["senior"]}, [],
                             [{"entity_id": "p2", "tags": ["wrong_seniority"], "state": "maybe"}], ROWS)
    assert "seniority" not in c["refine_facets"]
    assert "junior" in c["avoid_terms"]
    assert any("no longer gates" in e for e in c["edits"])


def test_wrong_company_target_removes_a_named_target_or_excludes_the_company():
    named = feedback_to_contract("ML at Stripe or Acme", {"company": ["stripe", "acme"]}, [],
                                 [{"entity_id": "p2", "tags": ["wrong_company_target"], "state": "unreviewed"}], ROWS)
    assert named["refine_facets"]["company"] == ["stripe"] and named["exclude_companies"] == []
    unnamed = feedback_to_contract("ML engineers", {}, [],
                                   [{"entity_id": "p2", "tags": ["wrong_company_target"], "state": "unreviewed"}], ROWS)
    assert unnamed["exclude_companies"] == ["Acme"]


def test_evidence_tags_toggle_the_requirement_both_ways():
    need = feedback_to_contract("ML engineers", {}, [], [{"entity_id": "p1", "tags": ["needs_artifact_evidence"]}], ROWS)
    assert need["evidence_kinds"] == ["paper", "repo", "post", "talk", "patent"]
    drop = feedback_to_contract("ML engineers", {}, ["repo"], [{"entity_id": "p1", "tags": ["private_company_talent"]}], ROWS)
    assert drop["evidence_kinds"] == []
    none = feedback_to_contract("ML engineers", {}, [], [{"entity_id": "p1", "tags": [], "state": "maybe"}], ROWS)
    assert none["edits"] == [] and none["question"] == "ML engineers"


def test_diff_rows_reports_added_removed_moved_and_evidence_changes():
    before = [{"entity_id": f"e{i}", "name": f"N{i}", "evidence": {"strength": "weak"}} for i in range(15)]
    after = [{"entity_id": "e14", "name": "N14", "evidence": {"strength": "strong"}}] + \
            [{"entity_id": f"e{i}", "name": f"N{i}", "evidence": {"strength": "weak"}} for i in range(1, 14)] + \
            [{"entity_id": "new", "name": "New", "evidence": {"strength": "weak"}}]
    d = diff_rows(before, after)
    assert d["n_added"] == 1 and d["added"][0]["name"] == "New"
    assert d["n_removed"] == 1 and d["removed"][0]["entity_id"] == "e0"
    assert d["n_moved"] == 1 and d["moved"][0] == {"entity_id": "e14", "name": "N14", "from": 15, "to": 1}
    assert d["evidence_changed"] == [{"entity_id": "e14", "name": "N14", "from": "weak", "to": "strong"}]
    txt = diff_text(d, ["HM wants more like N14: prefer x"])
    assert txt.startswith("15 people (was 15): 1 new, 1 dropped, 1 moved") and "Brief changes:" in txt


def test_fact_tags_map_one_to_one_onto_contract_edits():
    """The redesigned control: reviewers tap the card's OWN facts (prefer:/avoid: tags)."""
    from api.maps import derive_state, fact_tag, parse_fact_tag, valid_tag
    assert fact_tag("avoid", "seniority", "  Junior ") == "avoid:seniority=junior"
    assert parse_fact_tag("prefer:skill=pytorch") == ("prefer", "skill", "pytorch")
    assert valid_tag("avoid:company=acme") and valid_tag("more_like_this") and not valid_tag("avoid:x=y") and not valid_tag("drop table")
    fb = [{"entity_id": "p2", "tags": ["less_like_this", "avoid:seniority=junior", "avoid:company=acme"], "state": "not relevant", "reviewer_name": "HM"},
          {"entity_id": "p1", "tags": ["more_like_this", "prefer:skill=pytorch"], "state": "shortlist", "reviewer_name": "HM"},
          {"entity_id": "p3", "tags": ["avoid:evidence=weak"], "state": "needs more evidence"}]
    c = feedback_to_contract("ML engineers", {"seniority": ["senior"]}, [], fb, ROWS)
    assert c["exclude_ids"] == ["p2"] and c["exclude_companies"] == ["acme"]
    assert "junior" in c["avoid_terms"] and "seniority" not in c["refine_facets"]
    assert c["question"] == "ML engineers — prefer pytorch"          # a tapped fact, not the whole card
    assert "analytics" not in c["avoid_terms"]                        # bare-👎 demotion does not fire when facts were tapped
    assert c["evidence_kinds"] == []                                  # one weak-evidence tap is a row state, not a map rule
    assert any("HM: Bob's level (junior) is off" in e for e in c["edits"])
    assert any("HM: more like Ada's skill (pytorch)" in e for e in c["edits"])
    assert derive_state(["prefer:skill=pytorch"]) == "shortlist" and derive_state(["avoid:evidence=weak"]) == "needs more evidence"
    c2 = feedback_to_contract("ML engineers", {}, [], [{"entity_id": "p1", "tags": ["avoid:evidence=weak"]}, {"entity_id": "p3", "tags": ["avoid:evidence=weak"]}], ROWS)
    assert c2["evidence_kinds"] == ["paper", "repo", "post", "talk", "patent"]   # two rows → the map requires linked work


def test_job_feedback_taps_become_match_preferences():
    from api.calibration import job_feedback_to_prefs, row_ref
    rows = {"j1": {"id": "j1", "title": "Senior Backend Engineer", "company": "stripe", "location": "Austin, TX"},
            "j2": {"id": "j2", "title": "SDE II - Frontend", "company": "netomi", "location": "Remote"}}
    assert row_ref(rows["j1"]) == "j1" and row_ref({"company": "x", "title": "T", "location": "L"}) == "x|T|L"
    fb = [{"entity_id": "j2", "tags": ["less_like_this", "avoid:title=frontend", "avoid:company=netomi"], "state": "not relevant"},
          {"entity_id": "j1", "tags": ["more_like_this", "prefer:level=senior", "prefer:location=austin"], "state": "shortlist", "reviewer_name": "me"}]
    c = job_feedback_to_prefs("backend roles", fb, rows)
    p = c["prefs"]
    assert p["exclude_keywords"] == ["frontend"] and p["exclude_companies"] == ["netomi"]
    assert p["seniorities"] == ["senior"] and p["locations"] == ["austin"]
    assert c["exclude_refs"] == ["j2"]
    assert any("SDE II - Frontend @ netomi's title (frontend) is off — excluded" in e for e in c["edits"])
    assert any("me: more like Senior Backend Engineer @ stripe's level (senior)" in e for e in c["edits"])
    bare = job_feedback_to_prefs("backend roles", [{"entity_id": "j1", "tags": ["more_like_this"], "state": "shortlist"}], rows)
    assert bare["prefs"]["role_keywords"] == ["backend"]           # a bare 👍 prefers the title's own words
