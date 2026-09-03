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
