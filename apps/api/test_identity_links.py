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
