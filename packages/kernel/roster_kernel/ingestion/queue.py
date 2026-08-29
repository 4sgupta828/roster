"""Generic job queue — domain-free.

Models the claim/complete/fail-with-backoff contract the ingestion pipeline
needs. The production backend is Postgres `FOR UPDATE SKIP LOCKED`; the port
here is backend-agnostic, and `InMemoryJobQueue` is the offline/dev/test impl.
Job types are opaque strings (e.g. discover_entities|list_documents|
fetch_artifact) — the kernel never enumerates domain job types.
"""
from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: int
    job_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    run_after: int = 0          # logical clock tick; claim skips future jobs
    last_error: str = ""


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, job_type: str, payload: dict, *, priority: int = 0) -> int: ...
    def claim(self, *, now: int = 0) -> Job | None: ...
    def complete(self, job_id: int) -> None: ...
    def fail(self, job_id: int, error: str, *, max_attempts: int, backoff: int, now: int = 0) -> None: ...


class InMemoryJobQueue:
    """Deterministic in-memory queue for dev/tests.

    `now` is a caller-supplied logical clock (tests control time; no wall clock,
    keeping runs reproducible). Claim picks the highest-priority eligible PENDING
    job, oldest-id first.
    """

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)

    def enqueue(self, job_type: str, payload: dict, *, priority: int = 0) -> int:
        jid = next(self._ids)
        self._jobs[jid] = Job(id=jid, job_type=job_type, payload=dict(payload), priority=priority)
        return jid

    def claim(self, *, now: int = 0) -> Job | None:
        eligible = [
            j for j in self._jobs.values()
            if j.status is JobStatus.PENDING and j.run_after <= now
        ]
        if not eligible:
            return None
        job = sorted(eligible, key=lambda j: (-j.priority, j.id))[0]
        job.status = JobStatus.RUNNING
        job.attempts += 1
        return job

    def complete(self, job_id: int) -> None:
        self._jobs[job_id].status = JobStatus.DONE

    def fail(self, job_id: int, error: str, *, max_attempts: int, backoff: int, now: int = 0) -> None:
        job = self._jobs[job_id]
        job.last_error = error
        if job.attempts >= max_attempts:
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.PENDING           # retry
            job.run_after = now + backoff * job.attempts  # linear backoff

    # test/inspection helpers
    def get(self, job_id: int) -> Job:
        return self._jobs[job_id]

    def counts(self) -> dict[JobStatus, int]:
        out: dict[JobStatus, int] = {s: 0 for s in JobStatus}
        for j in self._jobs.values():
            out[j.status] += 1
        return out
