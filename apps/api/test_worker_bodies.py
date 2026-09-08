"""The worker's posting-bodies thread must run the ingest script LIVE — without --live the script is a
dry run, and the serial leg ran dry for a day (2026-09-04) while looking busy in the logs."""
import sys
import types


def test_body_backfill_thread_runs_live(monkeypatch):
    sys.path.insert(0, "apps")
    from worker import bulk_ingest as bi

    calls = []

    async def _not_stopped():
        return False

    class _Stop(Exception):
        pass

    def _sleep(_):
        raise _Stop()

    monkeypatch.setattr(bi, "_is_stopped", _not_stopped)
    monkeypatch.setattr(bi, "_run_chunk", lambda argv: calls.append(list(argv)))
    monkeypatch.setattr(bi.time, "sleep", _sleep)
    try:
        bi._body_backfill_loop(150, 30)
    except _Stop:
        pass
    assert calls == [["scripts/ingest_jobs.py", "--live", "--bodies", "150"]]
