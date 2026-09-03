"""ATS single-posting readers — pure, with a fake getter."""
import json

from api.ats_posting import ats_posting_text, greenhouse_token_candidates


def test_greenhouse_token_candidates_from_embedding_hosts():
    assert greenhouse_token_candidates("https://www.pinterestcareers.com/jobs/?gh_jid=8015529")[0] == "pinterest"
    assert "stripe" in greenhouse_token_candidates("https://careers.stripe.com/jobs?gh_jid=1")
    assert greenhouse_token_candidates("https://boards.greenhouse.io/tubi/jobs/123")[0] == "tubi"


def test_ats_posting_text_reads_greenhouse_lever_ashby():
    calls = []
    def get(u):
        calls.append(u)
        if "boards-api.greenhouse.io/v1/boards/pinterest/jobs/8015529" in u:
            return json.dumps({"title": "Software Engineer II", "location": {"name": "San Francisco, CA, US; Remote, US"},
                               "content": "&lt;p&gt;Build the &lt;b&gt;home feed&lt;/b&gt;. Requirements: Python, 3+ years.&lt;/p&gt;"})
        if "api.lever.co/v0/postings/acme/abc" in u:
            return json.dumps({"text": "Backend Engineer", "categories": {"location": "Austin", "commitment": "Full-time"},
                               "descriptionPlain": "Own the API.", "lists": [{"text": "Requirements", "content": "<li>Go</li><li>Postgres</li>"}]})
        if "api.ashbyhq.com/posting-api/job-board/acme" in u:
            return json.dumps({"jobs": [{"id": "j1", "title": "ML Engineer", "location": "Remote", "isRemote": True,
                                         "compensation": {"compensationTierSummary": "$180K – $220K"}, "descriptionPlain": "Train models."}]})
        return ""
    gh = ats_posting_text("https://www.pinterestcareers.com/jobs/?gh_jid=8015529", get)
    assert gh.startswith("Software Engineer II · San Francisco") and "home feed" in gh and "<" not in gh
    lv = ats_posting_text("https://jobs.lever.co/acme/abc", get)
    assert lv.startswith("Backend Engineer · Austin · Full-time") and "Go Postgres" in lv
    ab = ats_posting_text("https://jobs.ashbyhq.com/acme/j1", get)
    assert "ML Engineer · Remote · Remote · compensation $180K – $220K" in ab and "Train models" in ab
    assert ats_posting_text("https://example.com/careers/123", get) == ""


def test_closed_posting_is_reported_not_guessed():
    from api.ats_posting import CLOSED, NOT_FOUND
    def get(u):
        if u.endswith("/jobs/8015529"):
            return NOT_FOUND
        if u.endswith("/boards/pinterest"):
            return json.dumps({"name": "Pinterest"})
        if "api.lever.co" in u:
            return NOT_FOUND
        if "api.ashbyhq.com" in u:
            return json.dumps({"jobs": [{"id": "other", "title": "x"}]})
        return ""
    assert ats_posting_text("https://www.pinterestcareers.com/jobs/?gh_jid=8015529", get) == CLOSED
    assert ats_posting_text("https://jobs.lever.co/acme/gone", get) == CLOSED
    assert ats_posting_text("https://jobs.ashbyhq.com/acme/gone", get) == CLOSED
