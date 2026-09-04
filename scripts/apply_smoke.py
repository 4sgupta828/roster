"""APPLICATION-PLAN SMOKE across job boards — definition-driven, no browser, nothing submitted.
Fetches each ATS's own form definition for one live posting per board shape, binds a synthetic profile,
and prints coverage: questions, answered by source, required-but-open, draftable, policies.
Usage: PYTHONPATH=apps .venv/bin/python scripts/apply_smoke.py [url ...]"""
import asyncio, json, sys, time, urllib.request

PROFILE = {"first_name": "Test", "last_name": "Applicant", "email": "test-applicant@roster.test", "phone": "+1 555 0100", "city": "Austin", "region": "TX",
           "country": "United States", "postal_code": "78701", "linkedin": "https://www.linkedin.com/in/test-applicant", "github": "https://github.com/test-applicant",
           "current_company": "Acme", "current_title": "Backend Engineer", "years_experience": "5", "requires_sponsorship": "No", "us_authorized_to_work": "Yes",
           "preferred_name": "Test", "pronouns": "He/Him", "work_location": "Austin, TX, USA", "ack_privacy": "Yes", "ack_certify": "Yes", "marketing_opt_in": "No",
           "veteran_status": "Prefer not to say", "disability_status": "Prefer not to say", "gender": "Prefer not to say", "race_ethnicity": "Prefer not to say",
           "hispanic_latino": "No", "willing_to_relocate": "No", "remote_preference": "Remote", "desired_salary": "$180,000", "earliest_start_date": "2 weeks",
           "source": "Job board", "highest_degree": "Bachelor's Degree", "school": "State University", "previously_worked_here": "No"}


def _get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 roster-smoke"}), timeout=25))


def default_targets():
    t = []
    for label, fn in [("greenhouse job-boards", lambda: _get("https://boards-api.greenhouse.io/v1/boards/gusto/jobs")["jobs"][0]["absolute_url"]),
                      ("greenhouse via company page", lambda: "https://www.sofi.com/careers/job/?gh_jid=" + str(_get("https://boards-api.greenhouse.io/v1/boards/sofi/jobs")["jobs"][0]["id"])),
                      ("greenhouse embedded (gh_jid)", lambda: "https://www.pinterestcareers.com/jobs/?gh_jid=" + str(_get("https://boards-api.greenhouse.io/v1/boards/pinterest/jobs")["jobs"][0]["id"])),
                      ("lever", lambda: _get("https://api.lever.co/v0/postings/offchainlabs?mode=json")[0]["hostedUrl"]),
                      ("ashby", lambda: _get("https://api.ashbyhq.com/posting-api/job-board/sift")["jobs"][0]["jobUrl"]),
                      ("ashby (application url)", lambda: _get("https://api.ashbyhq.com/posting-api/job-board/d-matrix")["jobs"][0]["jobUrl"] + "/application")]:
        try: t.append((label, fn()))
        except Exception as e: print(f"{label} lookup failed: {e}")
    t.append(("workday (unplannable, expected)", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"))
    return t


async def run(label, url):
    from api.apply_adapters import fetch_form
    from api.apply_plan import bind_plan, draftable, summary
    t0 = time.time()
    f = await fetch_form(url)
    if not f:
        ok = "workday" in label
        print(f"[{label}] {'OK ' if ok else 'BAD'} {time.time()-t0:.1f}s no adapter / no form"); return ok
    p = bind_plan(f, PROFILE, {}, {}); s = summary(p); dr = draftable(p)
    hard = [b for b in s["blocking"] if b not in {d["label"] for d in dr}]   # required + open + NOT draftable = the user must answer
    ok = s["total"] >= 3 and s["answered"] >= 3
    print(f"[{label}] {'OK ' if ok else 'BAD'} {time.time()-t0:.1f}s {f['ats']}:{f['board']} q={s['total']} answered={s['answered']} {s['by_source']} required-open={len(s['blocking'])} draftable={len(dr)} needs-user={len(hard)}")
    print("      needs user:", [h[:45] for h in hard][:6])
    return ok


async def main():
    targets = [("arg", u) for u in sys.argv[1:]] or default_targets()
    results = [await run(l, u) for l, u in targets]
    print(f"\n{sum(results)}/{len(results)} boards OK (plans only; nothing submitted)")
    sys.exit(0 if all(results) else 1)

asyncio.run(main())
