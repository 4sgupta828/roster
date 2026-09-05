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


def test_role_keyword_matches_whole_words_only():
    # 'Director of Turbomachinery' was a "role match" for a CTO because 'cto' ⊂ 'direCTOr'
    import re
    rx = lambda k, title: bool(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", title.lower()))
    assert not rx("cto", "Director of Turbomachinery")
    assert rx("cto", "Founder & CTO") and rx("cto", "CTO / VP Engineering") and rx("vp engineering", "VP Engineering, Platform")


def test_a_posting_about_another_field_is_demoted_even_when_the_title_says_nothing():
    from api.people_population import family_of_body
    prof = text_families("Founder CTO. Distributed systems, Kubernetes, backend platform.")
    body = "Lead the turbomachinery team designing compressor and turbine stages; aerodynamic and thermodynamic analysis in ANSYS."
    assert family_of_body(body) == "mechanical / civil / electrical"
    pen, why = family_penalty(prof, "Director of Turbomachinery", body)
    assert pen == 0.20 and "different field" in why
    # a software posting that mentions one non-software term stays a software posting
    assert family_of_body("Build our billing platform in Go; partner with the finance controller on invoicing.") == ""
    assert family_penalty(prof, "Staff Software Engineer", "Build our billing platform in Go; partner with the finance controller.") == (0.0, "")
