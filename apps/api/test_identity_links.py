"""Cross-source identity links: accepted only on name + shared non-generic employer with a unique-enough name."""
from api.identity_links import acceptable


def test_link_acceptance_rules():
    ok = {"name": "Ada Byte", "employer": "anthropic", "n_a": 1, "n_b": 1}
    assert acceptable(ok)
    assert not acceptable({**ok, "employer": "freelance"})                 # generic employer proves nothing
    assert not acceptable({**ok, "employer": "ibm"})                       # too short to be distinctive here
    assert not acceptable({**ok, "n_a": 1, "n_b": 4})                      # 4 same-named openalex identities → ambiguous
    assert not acceptable({**ok, "name": "Ada"})                           # single-token name
    assert acceptable({**ok, "n_a": 3, "n_b": 3}) and not acceptable({**ok, "n_a": 3, "n_b": 3}, max_dupes=2)


def test_collapse_linked_one_card_per_person_never_by_name_alone():
    from api.people_population import collapse_linked
    rows = [
        {"entity_id": "github:mg", "name": "Mukul Gupta", "links": [{"kind": "github", "url": "https://github.com/mg"},
                                                                   {"kind": "linkedin_search", "url": "https://google/?q=x"}],
         "attributes": [{"key": "company", "display": "Cisco"}], "blurb": ""},
        {"entity_id": "openalex:A1", "name": "Mukul Gupta", "links": [{"kind": "linkedin", "url": "https://linkedin.com/in/mg"}],
         "attributes": [{"key": "company", "display": "cisco"}, {"key": "role", "display": "researcher"}], "blurb": "Researcher at Cisco"},
        {"entity_id": "github:mg2", "name": "Mukul Gupta", "links": [], "attributes": [{"key": "company", "display": "Infosys"}]},
    ]
    out = collapse_linked(rows, {"github:mg": ["openalex:A1"], "openalex:A1": ["github:mg"]})
    assert [r["entity_id"] for r in out] == ["github:mg", "github:mg2"]      # unlinked namesake stays
    kept = out[0]
    assert kept["merged_identities"] == ["openalex:A1"]
    kinds = [l["kind"] for l in kept["links"]]
    assert "linkedin" in kinds and "github" in kinds and "linkedin_search" not in kinds
    assert [a["display"] for a in kept["attributes"]] == ["Cisco", "researcher"]   # deduped case-insensitively
    assert kept["blurb"] == "Researcher at Cisco"
    assert collapse_linked(rows, {}) is rows
