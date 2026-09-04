"""AUTO-APPLY SMOKE across job boards — FILL ONLY, never submits. Runs the agent's `fill` mode on one live
posting per ATS with a synthetic profile and prints what got filled / left open / blocked. Usage:
    PYTHONPATH=apps .venv/bin/python scripts/apply_smoke.py [url ...]
Default targets are looked up live from public board APIs (Greenhouse, Lever, Ashby) plus fixed samples."""
import base64, json, os, subprocess, sys, tempfile, time, urllib.request

PROFILE = {"first_name": "Test", "last_name": "Applicant", "email": "test-applicant@roster.test", "phone": "+1 555 0100",
           "city": "Austin", "region": "TX", "country": "United States", "linkedin": "https://www.linkedin.com/in/test-applicant",
           "github": "https://github.com/test-applicant", "current_company": "Acme", "current_title": "Backend Engineer",
           "years_experience": "5", "requires_sponsorship": "No", "us_authorized_to_work": "Yes", "preferred_name": "Test",
           "pronouns": "He/Him", "work_location": "Austin, TX, USA", "ack_privacy": "Yes", "ack_certify": "Yes",
           "veteran_status": "Prefer not to say", "disability_status": "Prefer not to say", "gender": "Prefer not to say",
           "race_ethnicity": "Prefer not to say", "hispanic_latino": "No", "willing_to_relocate": "No", "remote_preference": "Remote",
           "desired_salary": "$180,000", "earliest_start_date": "2 weeks", "source": "Job board", "highest_degree": "B.S.", "school": "State University"}


def _get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 roster-smoke"}), timeout=25))


def default_targets():
    t = []
    try: t.append(("greenhouse job-boards", _get("https://boards-api.greenhouse.io/v1/boards/gusto/jobs")["jobs"][0]["absolute_url"]))
    except Exception as e: print("gusto lookup failed", e)
    try: t.append(("greenhouse boards", _get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs")["jobs"][0]["absolute_url"]))
    except Exception as e: print("stripe lookup failed", e)
    try: t.append(("lever", _get("https://api.lever.co/v0/postings/offchainlabs?mode=json")[0]["hostedUrl"]))
    except Exception as e: print("lever lookup failed", e)
    try: t.append(("ashby", _get("https://api.ashbyhq.com/posting-api/job-board/sift")["jobs"][0].get("applyUrl") or _get("https://api.ashbyhq.com/posting-api/job-board/sift")["jobs"][0]["jobUrl"]))
    except Exception as e: print("ashby lookup failed", e)
    try: t.append(("ashby (posting url)", _get("https://api.ashbyhq.com/posting-api/job-board/level")["jobs"][0]["jobUrl"]))
    except Exception as e: print("level lookup failed", e)
    t.append(("greenhouse embedded (gh_jid)", "https://www.pinterestcareers.com/jobs/?gh_jid=" + str(_get("https://boards-api.greenhouse.io/v1/boards/pinterest/jobs")["jobs"][0]["id"])))
    try: t.append(("smartrecruiters", _get("https://api.smartrecruiters.com/v1/companies/smartrecruiters/postings")["content"][0]["applyUrl"]))
    except Exception as e: print("smartrecruiters lookup failed", e)
    t.append(("workday (needs_you expected)", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"))
    return t


def run(label, url):
    with tempfile.TemporaryDirectory() as d:
        inp, outp = os.path.join(d, "in.json"), os.path.join(d, "out.json")
        json.dump({"url": url, "profile": PROFILE, "answers": {}, "resume": {"name": "cv.txt", "b64": base64.b64encode(b"Test Applicant\nBackend engineer, 5 years.").decode()}}, open(inp, "w"))
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "api.auto_apply", "fill", inp, outp], capture_output=True, text=True, timeout=240,
                           env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "apps")})
        dt = time.time() - t0
        if r.returncode != 0 or not os.path.exists(outp):
            print(f"[{label}] CRASH rc={r.returncode} {dt:.0f}s {r.stderr[-200:]}"); return False
        o = json.load(open(outp))
        req_open = [q for q in o.get("open") or [] if q.get("required") and not q.get("voluntary")]
        ok = o.get("status") in ("filled", "needs_you") and (o.get("filled") or o.get("status") == "needs_you")
        print(f"[{label}] {'OK ' if ok else 'BAD'} {dt:.0f}s status={o.get('status')} filled={len(o.get('filled') or [])} open={len(o.get('open') or [])} "
              f"required-open={len(req_open)} captcha={o.get('captcha')} reason={(o.get('reason') or '')[:70]!r}")
        print("      filled:", [f["label"][:24] for f in (o.get("filled") or [])][:10])
        print("      required open:", [q["label"][:45] for q in req_open][:6], "| notes:", (o.get("notes") or [])[:2])
        return ok


if __name__ == "__main__":
    targets = [("arg", u) for u in sys.argv[1:]] or default_targets()
    results = [bool(run(l, u)) for l, u in targets]
    print(f"\n{sum(results)}/{len(results)} boards OK (fill-only; nothing submitted)")
    sys.exit(0 if all(results) else 1)
