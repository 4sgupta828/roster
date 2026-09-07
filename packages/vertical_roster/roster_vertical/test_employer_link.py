"""The hiring company's own board, derived from the posting URL — never a guess (owner, 2026-09-07)."""
from roster_vertical.employer_link import employer_page

REAL = [  # shapes taken from prod, one per ATS actually in the corpus
    ("https://jobs.ashbyhq.com/vori/68decf7b-c98d-44cc-bb0c-5164d97d8728", "https://jobs.ashbyhq.com/vori"),
    ("https://jobs.lever.co/fullscript/9cd60056-bc76-4b43-8410-b41970bffd5b", "https://jobs.lever.co/fullscript"),
    ("https://app.careerpuck.com/job-board/lyft/job/8728414002?gh_jid=8728414002", "https://app.careerpuck.com/job-board/lyft"),
    ("https://jobs.smartrecruiters.com/accenturefederalservices/96343863", "https://jobs.smartrecruiters.com/accenturefederalservices"),
    ("https://7eleven.wd3.myworkdayjobs.com/7eleven/job/QLD---Sunshine-Coast/Team-Member", "https://7eleven.wd3.myworkdayjobs.com/7eleven"),
    ("https://boards.greenhouse.io/acme/jobs/4567", "https://boards.greenhouse.io/acme"),
]


def test_every_ats_in_the_corpus_yields_the_employers_own_board():
    for url, want in REAL:
        assert employer_page(url) == want, url


def test_a_url_that_names_no_employer_yields_nothing_rather_than_a_guess():
    for url in ("https://apply.workable.com/j/6EDFC2AB01",          # the employer is not in the path
                "https://www.linkedin.com/jobs/view/123",            # not an employer board
                "https://jobs.ashbyhq.com/", "https://jobs.lever.co/job/123",
                "ftp://jobs.lever.co/acme/1", "not a url", "", None):
        assert employer_page(url) is None, url
