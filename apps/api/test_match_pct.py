"""Match percent + field demotion (owner, 2026-09-05: a pharma director role showed 55% for a Founder CTO
while the résumé fit was 0%)."""
from api.people_population import (_DEFAULT_SIM_FLOOR, calibrated_pct, family_mismatch, family_penalty,
                                   text_families)


def test_percent_is_measured_above_the_resumes_noise_floor():
    # measured on prod 2026-09-05: nearest-400 similarities 0.545–0.624, random postings p90 ≈ 0.40
    floor = 0.40
    assert calibrated_pct(0.545, floor) == 58 and calibrated_pct(0.624, floor) == 90
    assert calibrated_pct(0.40, floor) == 1 and calibrated_pct(0.28, floor) == 1      # noise reads as ~0, never negative
    assert calibrated_pct(0.70, floor) == 99                                            # capped
    assert calibrated_pct(0.55) == calibrated_pct(0.55, _DEFAULT_SIM_FLOOR)            # default floor when no sample


def test_a_title_from_another_field_is_named_and_demoted_harder_than_another_engineering_discipline():
    prof = text_families("Founder CTO. Distributed systems, Kubernetes, backend platform, LLM search.")
    assert "backend / infra" in prof
    pen, why = family_penalty(prof, "Senior Director, Clinical Translational Scientist")
    assert pen == 0.20 and why == "title says clinical / pharma — a different field"
    pen2, why2 = family_penalty(prof, "Senior Frontend Engineer")
    assert pen2 == 0.10 and why2 == "title says frontend"
    assert family_penalty(prof, "Staff Software Engineer") == (0.0, "")            # unstated → left alone
    assert family_penalty(set(), "Clinical Scientist") == (0.0, "")                 # profile states nothing → no demotion
    assert family_mismatch(prof, "Account Executive, Enterprise Sales") == "sales"


def test_field_penalty_moves_the_displayed_percent_to_the_floor():
    # the pharma role: pool-floor similarity minus the cross-field penalty reads as ~0, not 55
    assert calibrated_pct(0.55 - 0.20, 0.40) == 1
    assert calibrated_pct(0.55, 0.40) == 60
