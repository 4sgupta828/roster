from roster_vertical.job_grouping import GROUP_DIMENSIONS, resegment_prompt, row_line, segment_prompt, tokens


def test_the_tokens_describe_the_work_never_the_packaging():
    row = {"title": "Senior Staff Machine Learning Engineer II (Remote)", "company": "acme",
           "facets": {"role_family": ["machine learning engineer"], "specialty": ["ml infrastructure"], "skill": ["kubernetes", "python"]}}
    t = tokens(row)
    assert "ml infrastructure" in t and "kubernetes" in t and "machine" in t
    for junk in ("senior", "staff", "engineer", "remote", "ii", "acme"):
        assert junk not in t, junk


def test_the_line_the_model_sees_carries_no_employer_or_place():
    row = {"title": "Backend Engineer, Core APIs", "company": "stripe", "location": "New York",
           "facets": {"specialty": ["payments"], "skill": ["go", "postgres"], "comp": ["200k_300k"]}}
    line = row_line(3, row)
    assert line.startswith("3| Backend Engineer, Core APIs") and "payments" in line and "go, postgres" in line
    assert "stripe" not in line and "New York" not in line and "200k" not in line


def test_the_guidance_forbids_the_cuts_that_are_not_distinctions():
    p = segment_prompt()
    for forbidden in ("employer", "seniority", "location", "employment type", "nearly every row shares"):
        assert forbidden in p, forbidden
    assert "AT MOST ONE group" in p and "Everything else" in p
    r = resegment_prompt("Account Executive", 49, 78)
    assert "49 of 78" in r and "Account Executive" in r and "more than half" in r
    assert [d[0] for d in GROUP_DIMENSIONS][:3] == ["auto", "company", "company_industry"]
    kinds = {d[0]: d[3] for d in GROUP_DIMENSIONS}
    assert kinds["company"] == "identity" and kinds["metro"] == "identity"        # many groups is the point
    assert kinds["level"] == "categorical" and kinds["company_industry"] == "categorical"


def test_a_shortlist_of_people_is_segmented_as_people_not_as_postings():
    jobs, people = segment_prompt(), segment_prompt(kind="person")
    assert "JOB SEEKER decides where to apply" in jobs and "CANDIDATES" in people and "HIRING MANAGER" in people
    for forbidden in ("employer", "seniority", "location"):
        assert forbidden in people, forbidden
    assert "CANDIDATES" in resegment_prompt("Backend", 40, 60, kind="person")
