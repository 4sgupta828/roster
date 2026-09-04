"""Job-seeker intake (tester feedback 2026-09-04): a self-description / attached résumé is a PROFILE,
never a free-text query; stated seniority ranks the agentic search; titles without a level are unknown."""
import asyncio
import base64


def _run(coro):
    """Run a coroutine on a fresh loop that STAYS current (asyncio.run() would leave later tests loop-less)."""
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

from api.media import attachment_texts
from api.people_population import (_title_level, is_self_description, wanted_levels, years_to_levels)


def test_self_description_is_detected_and_plain_queries_are_not():
    assert is_self_description("SDE/sre roles for me. I’m a software engineer with 5 years of experience. Java, Kubernetes")
    assert is_self_description("Can I give you my resume and on the basis of that can you give job listings for me?")
    assert is_self_description("I have 7 years of experience in data engineering")
    assert not is_self_description("remote backend roles at big tech")
    assert not is_self_description("machine learning engineer jobs in Austin")


def test_years_and_stated_seniority_become_level_preferences():
    assert years_to_levels("software engineer with 5 years of experience") == {"mid", "senior"}
    assert years_to_levels("new grad, 1 year") == {"junior"}
    assert years_to_levels("12+ years") == {"senior", "staff_plus"}
    assert years_to_levels("no years here") == set()
    assert wanted_levels("senior") == {"senior"} and wanted_levels("staff/principal") == {"staff_plus"}
    assert wanted_levels("") == set()


def test_title_level_is_unknown_when_the_title_says_nothing():
    assert _title_level("Senior Software Engineer") == ("senior", True)
    assert _title_level("Staff Engineer, Infrastructure") == ("staff_plus", True)
    assert _title_level("Software Engineer") == ("mid", False)      # unknown, never a confident 'mid'


def test_attachment_texts_reads_text_files_and_skips_images():
    cv = "Jane Doe\nWork experience\nBackend engineer at Acme, 5 years. Skills: Java, Kubernetes"
    atts = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(cv.encode()).decode()},
            {"name": "pic.png", "media_type": "image/png", "data": base64.b64encode(b"\x89PNG").decode()}]
    out = attachment_texts(atts)
    assert out == [("cv.txt", cv)]


def test_agentic_search_ranks_the_stated_level_and_demotes_a_far_one(monkeypatch):
    from api import people_population as pp
    from roster_kernel.providers.llm import LLMResult

    class _LLM:
        async def complete(self, **kw):
            return LLMResult(parsed=pp._JobPlan(intent="senior backend", query_variants=[], must_have=["engineer"],
                                                seniority="senior"), output_tokens=1)

    class _Store:
        async def match_jobs_scored(self, qv, cap=60):
            return [{"id": "a", "company": "x", "title": "Senior Backend Engineer", "location": "Austin, TX", "sim": 0.80},
                    {"id": "b", "company": "y", "title": "Backend Engineer Intern", "location": "Austin, TX", "sim": 0.84},
                    {"id": "c", "company": "z", "title": "Backend Engineer", "location": "Austin, TX", "sim": 0.82}]

    monkeypatch.setattr(pp, "embed_query", lambda q: [0.1, 0.2])
    res = _run(pp.agentic_job_search(_Store(), "senior backend engineer roles", _LLM()))
    order = [j["id"] for j in res["jobs"]]
    assert order[0] == "a" and order[-1] == "b", order          # stated level leads; an intern posting sinks
    by = {j["id"]: j for j in res["jobs"]}
    assert by["a"]["seniority"] == "senior" and by["a"]["level_source"] == "title"
    assert by["c"]["seniority"] == "" and by["c"]["level_source"] == "unknown"


def test_family_mismatch_is_soft_and_only_fires_on_stated_families():
    from api.people_population import family_mismatch, text_families
    prof = text_families("software engineer, 5 years, Java, Kubernetes, SRE, databases, linux")
    assert prof == {"backend / infra"}
    assert family_mismatch(prof, "SDE I - Frontend") == "frontend"
    assert family_mismatch(prof, "Security Engineer") == "security"
    assert family_mismatch(prof, "Software Engineer") == ""                  # unstated title: left alone
    assert family_mismatch(prof, "Infrastructure Software Engineer") == ""   # same family
    assert family_mismatch(set(), "SDE I - Frontend") == ""                  # profile names nothing: no demotion
    full = text_families("full-stack: React frontend and Go backend")
    assert family_mismatch(full, "Frontend Engineer") == "" and family_mismatch(full, "Backend Engineer") == ""


def test_job_brief_contract_mirrors_the_talent_map_contract():
    from api.people_population import job_brief_contract
    # agentic free-text search: company named → hard; must-have words + level → soft; angles listed
    c = job_brief_contract(question="senior backend roles at Stripe", query={"company": ["stripe"], "title_keywords": ["backend"], "location": ""},
                           plan={"variants": ["backend engineer", "platform engineer"], "intent": "senior backend at Stripe",
                                 "must_have": ["backend", "engineer"], "seniority": "senior"}, job_must=["remote"], scope={"label": "SF Bay Area"})
    assert c["hard"] == {"company": ["stripe"], "must have": ["remote"], "scope": ["SF Bay Area"]}
    assert c["soft"] == {"title words": ["backend", "engineer"], "level": ["senior"]}
    assert c["angles"] == ["backend engineer", "platform engineer"] and c["assumptions"] == []
    assert "angles" in c["ranking"]
    # résumé path: the profile read is code-derived (years → level preference, discipline, skills)
    p = job_brief_contract(question="roles for me", profile_text="Backend engineer, 5 years, Java, Kubernetes, Postgres, Linux", matched_on="description")
    assert p["profile"]["years"] == "5" and p["profile"]["level_pref"] == ["mid", "senior"]
    assert p["profile"]["families"] == ["backend / infra"] and "java" in p["profile"]["skills"] and "kubernetes" in p["profile"]["skills"]
    assert p["soft"]["discipline"] == ["backend / infra"] and p["soft"]["level"] == ["mid", "senior"]
    assert "profile" in p["ranking"]
    # nothing stated → the assumptions say so instead of a confident filter
    n = job_brief_contract(question="engineering jobs", query={"company": [], "title_keywords": ["engineering"], "location": ""})
    assert n["hard"] == {} and n["assumptions"][0].startswith("level not stated")


def test_pdf_attachment_is_read_docling_first_then_text_layer(monkeypatch):
    """A PDF résumé attachment yields text: docling (separate process) when available, else the text layer."""
    import asyncio, base64
    import fitz
    from api import media
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((50, 72), "Jane Doe\nWork experience\nBackend Engineer, Acme, 5 years. Skills: Java, Kubernetes\nEducation\nB.S.", fontsize=10)
    att = {"name": "cv.pdf", "media_type": "application/pdf", "data": base64.b64encode(doc.tobytes()).decode()}
    monkeypatch.setattr(media, "_docling_markdown_subprocess", lambda data, name: "")     # docling unavailable here
    out = _run(media.attachment_texts_async([att]))
    assert len(out) == 1 and "Kubernetes" in out[0][1] and out[0][0] == "cv.pdf"
    monkeypatch.setattr(media, "_docling_markdown_subprocess", lambda data, name: "# Jane Doe\n\n## Work experience\n\nBackend Engineer")
    out2 = _run(media.attachment_texts_async([att]))
    assert out2[0][1].startswith("# Jane Doe")                                            # docling wins when it answers
