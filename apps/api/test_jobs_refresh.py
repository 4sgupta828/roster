"""Jobs REFRESH (scripts/ingest_jobs.py): diff by the ATS's own id, adaptive cadence, closed postings out
of search. Pure-function tests; the DB path is exercised by the prod dry run."""
import datetime as dt
import importlib.util
import pathlib
import sys


def _ij():
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ingest_jobs.py"
    spec = importlib.util.spec_from_file_location("ingest_jobs", p)
    m = importlib.util.module_from_spec(spec); sys.modules["ingest_jobs"] = m; spec.loader.exec_module(m)
    return m


def test_plan_refresh_diffs_by_ats_id_not_title():
    ij = _ij()
    existing = {"1": "2026-09-01T00:00:00Z", "2": "2026-08-01T00:00:00Z", "3": "2026-07-01T00:00:00Z"}
    listed = [{"ats_id": "1", "changed_at": "2026-09-01T00:00:00Z", "title": "Renamed title"},   # same id, same stamp → untouched
              {"ats_id": "2", "changed_at": "2026-09-03T00:00:00Z"},                             # stamp moved → changed
              {"ats_id": "9", "changed_at": "2026-09-04T00:00:00Z"},                             # new
              {"title": "no id at all"}]                                                       # no id → treated as new
    new, changed, gone = ij.plan_refresh(existing, listed)
    assert [r.get("ats_id") for r in new] == ["9", None]
    assert [r["ats_id"] for r in changed] == ["2"]
    assert gone == ["3"]


def test_cadence_backs_off_for_quiet_boards_and_tightens_on_change():
    ij = _ij()
    now = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
    assert ij.next_check_after(None, now, changed=False) == 6 * 3600                      # never seen change → 6h
    assert ij.next_check_after(now - dt.timedelta(days=30), now, changed=True) == 6 * 3600   # just changed → 6h
    assert ij.next_check_after(now - dt.timedelta(days=2), now, changed=False) == 86400       # quiet 2 days → 1 day
    assert ij.next_check_after(now - dt.timedelta(days=60), now, changed=False) == 7 * 86400  # quiet 2 months → weekly cap
    assert ij.next_check_after(now - dt.timedelta(hours=1), now, changed=False) == 6 * 3600   # floor


def test_fetchers_carry_the_ats_id_and_change_stamp():
    ij = _ij()
    gh = ij._rows_greenhouse({"jobs": [{"id": 77, "title": "SWE", "updated_at": "2026-09-01T10:00:00-04:00", "location": {"name": "NYC"},
                                        "absolute_url": "https://x/77", "company_name": "Acme", "content": "<p>Hi</p>"}]})
    assert gh[0]["ats_id"] == "77" and gh[0]["changed_at"].startswith("2026-09-01") and gh[0]["company_name"] == "Acme"
    lv = ij._rows_lever([{"id": "abc", "text": "SWE", "createdAt": 1756700000000, "categories": {"location": "Remote"}, "hostedUrl": "https://l/abc"}])
    assert lv[0]["ats_id"] == "abc" and lv[0]["changed_at"] == "1756700000000"
    ab = ij._rows_ashby({"jobs": [{"id": "u-1", "title": "SWE", "publishedAt": "2026-08-30T00:00:00.000Z", "location": "SF", "jobUrl": "https://a/u-1"}]})
    assert ab[0]["ats_id"] == "u-1" and ab[0]["changed_at"].startswith("2026-08-30")


def test_search_queries_exclude_closed_postings():
    src = (pathlib.Path(__file__).resolve().parent / "claimgraph.py").read_text()
    body = src[src.index("async def search_jobs("):src.index("async def match_jobs_scored(") + 900]
    # every rs_job read path in the jobs search family carries the closed filter
    assert body.count("closed_at IS NULL") >= 5
