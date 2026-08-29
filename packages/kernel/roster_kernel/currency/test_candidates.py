"""Edition-candidate generation — held-out contract tests (the structural half of the
supersession judge; the LLM half is approval-gated and tested by its own eval cases)."""
from roster_kernel.currency.candidates import edition_candidates


def _d(did, issuer, year, conds):
    return {"document_id": did, "title": did, "issuer": issuer, "year": year, "conditions": conds}


def test_pairs_same_issuer_overlapping_subjects_different_years():
    docs = [_d("g:anemia-2012", "KDIGO", "2012", ["anemia in ckd", "esa"]),
            _d("g:anemia-2026", "KDIGO", "2026", ["anemia in ckd", "iron deficiency"])]
    pairs = edition_candidates(docs)
    assert len(pairs) == 1
    old, new = pairs[0]
    assert old["document_id"] == "g:anemia-2012" and new["document_id"] == "g:anemia-2026"


def test_never_pairs_across_issuers_same_year_or_disjoint_subjects():
    docs = [_d("a", "KDIGO", "2012", ["anemia in ckd"]),
            _d("b", "NICE", "2026", ["anemia in ckd"]),          # different issuer
            _d("c", "KDIGO", "2012", ["anemia in ckd", "esa"]),  # same year as a
            _d("d", "KDIGO", "2026", ["hypertension"])]          # disjoint subjects vs a
    assert edition_candidates(docs) == []


def test_decided_pairs_are_excluded_and_missing_metadata_skipped():
    docs = [_d("old", "KDIGO", "2012", ["ckd"]), _d("new", "KDIGO", "2026", ["ckd"]),
            _d("noyear", "KDIGO", "", ["ckd"]), _d("noissuer", "", "2020", ["ckd"])]
    assert edition_candidates(docs, exclude_pairs={("old", "new")}) == []
    assert len(edition_candidates(docs)) == 1                    # only the valid pair
