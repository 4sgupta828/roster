"""Durable BULK-INGEST loop for the worker service (panel Option C).

Runs the committed, resumable ingest engines (`scripts/ingest_jobs.py`, `scripts/ingest_people.py`)
in bounded chunks on an interval, off the API process so the multi-day grind never touches request
latency. Each engine checkpoints + upserts idempotently, so a crash/redeploy just resumes.

Enabled with ROSTER_BULK_INGEST=1 on a worker service (ROSTER_ROLE=worker). Knobs (env):
  ROSTER_BULK_INGEST_PEOPLE   run the PEOPLE grind too (default OFF — gated separately pending the
                              GitHub-ToS decision; jobs run without it)
  ROSTER_BULK_JOBS_CHUNK      max job boards/aggregators per cycle (0 = all remaining)
  ROSTER_BULK_PEOPLE_CHUNK    max people per cycle (default 500 — bounds GitHub + LLM/embed spend/cycle)
  ROSTER_BULK_PEOPLE_PERWINDOW  per search-window depth (default 1000)
  ROSTER_BULK_SWEEP_TOP       job-board sweep each cycle over SEC top-2000 + the N most-staffed
                              companies in the people index (jobs_sweep.py --universe all); 0 = off
  ROSTER_BULK_ARTIFACTS_CHUNK  people per source per cycle for the public-artifact linker
  ROSTER_BULK_EMBED_BACKFILL  jobs per cycle given a missing embedding (default 20000; pennies)
                              (scripts/ingest_artifacts.py: papers/repos/orgs by identity key; HTTP
                              only, no LLM spend). 0 = off (default).
  ROSTER_BULK_INTERVAL_SEC    seconds between cycles (default 900) — paces GitHub's 5k/hr + 30/min
  ROSTER_BULK_MAX_CYCLES_DAY  coarse daily cap on cycles (default 96 = one per ~15min) — cost ceiling

KILL SWITCH / observability: a control row in rs_ingest_checkpoint (source='control', cursor_key='stop').
Set status='stop' to pause the loop with no redeploy; 'run' (or absent) resumes. Progress is visible in
the same table (per board/window rows) — query it or add an /admin view.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time


async def _is_stopped() -> bool:
    """True if an operator paused the loop via the control row (no redeploy needed)."""
    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        return False
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS rs_ingest_checkpoint (
                 source text NOT NULL, cursor_key text NOT NULL, status text NOT NULL DEFAULT 'done',
                 n_seen int NOT NULL DEFAULT 0, n_written int NOT NULL DEFAULT 0,
                 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (source, cursor_key))""")
        row = await conn.fetchval(
            "SELECT status FROM rs_ingest_checkpoint WHERE source='control' AND cursor_key='stop'")
        return row == "stop"
    finally:
        await conn.close()


def _run_chunk(argv: list[str]) -> None:
    """Run one ingest-engine chunk as a subprocess (the scripts ARE the source of truth; no re-impl).
    Inherits the worker's env (ROSTER_CORPUS_DSN, OPENAI/DEEPSEEK, ROSTER_GITHUB_TOKEN). Never raises —
    a chunk failure is logged and the loop continues (next cycle resumes from the checkpoint)."""
    try:
        subprocess.run([sys.executable, *argv], cwd="/app", check=False, timeout=3600)
    except Exception as e:   # noqa: BLE001
        print(f"[bulk] chunk failed {argv}: {e}", flush=True)


def run_bulk_ingest_loop() -> None:
    people_on = os.environ.get("ROSTER_BULK_INGEST_PEOPLE", "").lower() in ("1", "true", "yes")
    jobs_chunk = int(os.environ.get("ROSTER_BULK_JOBS_CHUNK", "0") or 0)
    people_chunk = int(os.environ.get("ROSTER_BULK_PEOPLE_CHUNK", "500") or 500)
    # public-artifact linking (papers/repos/orgs by identity key): people per source per cycle; 0 = off
    artifacts_chunk = int(os.environ.get("ROSTER_BULK_ARTIFACTS_CHUNK", "0") or 0)
    # job-board SWEEP (7 ATS + Workday discovery over the SEC top-2000 + the N most-staffed companies
    # in the people index): resumable via rs_ats_probed, so each cycle only probes new pairs; 0 = off
    sweep_top = int(os.environ.get("ROSTER_BULK_SWEEP_TOP", "0") or 0)
    embed_backfill = int(os.environ.get("ROSTER_BULK_EMBED_BACKFILL", "20000") or 20000)
    per_window = int(os.environ.get("ROSTER_BULK_PEOPLE_PERWINDOW", "1000") or 1000)
    interval = int(os.environ.get("ROSTER_BULK_INTERVAL_SEC", "900") or 900)
    max_cycles_day = int(os.environ.get("ROSTER_BULK_MAX_CYCLES_DAY", "96") or 96)
    print(f"[bulk] loop start — people={people_on} jobs_chunk={jobs_chunk or 'all'} "
          f"people_chunk={people_chunk} interval={interval}s max_cycles/day={max_cycles_day}", flush=True)
    cycles_today, day_start = 0, time.monotonic()
    while True:
        if time.monotonic() - day_start >= 86400:
            cycles_today, day_start = 0, time.monotonic()
        if cycles_today >= max_cycles_day:
            time.sleep(interval); continue
        try:
            stopped = asyncio.run(_is_stopped())
        except Exception:   # noqa: BLE001 — DB blip must not kill the loop
            stopped = False
        if stopped:
            print("[bulk] paused (control row status=stop)", flush=True)
            time.sleep(interval); continue
        cycles_today += 1
        jobs_argv = ["scripts/ingest_jobs.py", "--live"] + (["--limit", str(jobs_chunk)] if jobs_chunk else [])
        _run_chunk(jobs_argv)
        # self-heal: jobs written without a vector (embed hiccups during big sweeps) are invisible to
        # résumé matching — give them their embedding (~$0.0004 / 1k jobs)
        _run_chunk(["scripts/ingest_jobs.py", "--backfill", str(embed_backfill)])
    body_boards = int(os.environ.get("ROSTER_BULK_BODY_BACKFILL", "0") or 0)
    if body_boards:      # POSTING BODIES: refetch N already-done ATS boards with content, store body + skills, re-embed
        _run_chunk(["scripts/ingest_jobs.py", "--bodies", str(body_boards)])
        if people_on:
            _run_chunk(["scripts/ingest_people.py", "--live", "--limit", str(people_chunk),
                        "--per-window", str(per_window)])
            # self-heal: embed any people written without a vector (embed hiccups) so they're searchable
            _run_chunk(["scripts/ingest_people.py", "--backfill", str(people_chunk)])
        if artifacts_chunk:
            _run_chunk(["scripts/ingest_artifacts.py", "--source", "all", "--limit", str(artifacts_chunk)])
            # cross-source identity links (name + shared employer, unique-name guarded) — one SQL pass
            _run_chunk(["scripts/link_identities.py"])
        if sweep_top:
            _run_chunk(["scripts/jobs_sweep.py", "--live", "--universe", "all", "--top", str(sweep_top),
                        "--workday", "--concurrency", "8"])
        time.sleep(interval)
