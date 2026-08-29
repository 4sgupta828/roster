"""Answer-video add-on — a COMPLETELY SEPARATE, flag-gated router.

It turns a delivered {question, answer} into a short narrated explainer mp4 by shelling
out to the IN-REPO, self-contained video generator (`apps/video/bin/answer-video.ts`).
There is NO cross-repo dependency. It does NOT import or touch the kernel research core,
retrieval, or providers — the research path runs identically whether this add-on is present
or not.

Default OFF (Rule 20): enable with ROSTER_VIDEO_ENABLED=true. Generation spends OpenAI/
Anthropic credits (storyboard LLM + TTS), so it is opt-in and never on the answer path.

Config (env):
  ROSTER_VIDEO_ENABLED   "true" to mount the router (default off).
  ROSTER_VIDEO_DIR       the in-repo video app dir (default <repo>/apps/video).
  ROSTER_VIDEO_TSX       tsx binary (default <video_dir>/node_modules/.bin/tsx).

Generation is async (a job runs in the background ~2 min); the UI polls status then
streams the file.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Optional async hook to link a finished video back to its saved Q&A session:
#   attach(session_id, filename=..., title=..., duration=...) -> None
AttachVideo = Callable[..., Awaitable[None]]


def video_enabled() -> bool:
    return os.environ.get("ROSTER_VIDEO_ENABLED", "").lower() in ("1", "true", "yes")


def _video_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / "video"   # <repo>/apps/video
    return Path(os.environ.get("ROSTER_VIDEO_DIR", str(default)))


def _out_dir() -> Path:
    return _video_dir() / "out"


def _tsx() -> str:
    return os.environ.get("ROSTER_VIDEO_TSX", str(_video_dir() / "node_modules" / ".bin" / "tsx"))


# --- durable video storage (R2) ---------------------------------------------
# The generator writes to apps/video/out, which is EPHEMERAL in a container (wiped on
# redeploy). So we also mirror each mp4 to R2 and serve from there when the local file is
# gone — this keeps past-session videos playable across deploys.
def _r2():
    bucket = os.environ.get("R2_BUCKET")
    if not bucket:
        return None, None
    try:
        import boto3
        client = boto3.client(
            "s3", endpoint_url=os.environ.get("R2_ENDPOINT"),
            aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
            region_name="auto")
        return client, bucket
    except Exception:
        return None, None


def _r2_put_video(name: str) -> None:
    client, bucket = _r2()
    path = _out_dir() / name
    if not client or not path.exists():
        return
    with open(path, "rb") as f:
        client.put_object(Bucket=bucket, Key=f"videos/{name}", Body=f.read(),
                          ContentType="video/mp4")


def _r2_get_video(name: str) -> bytes | None:
    client, bucket = _r2()
    if not client:
        return None
    try:
        return client.get_object(Bucket=bucket, Key=f"videos/{name}")["Body"].read()
    except Exception:
        return None


def video_exists(name: str) -> bool:
    """True if the mp4 is servable — present locally OR in R2. Used to hide videos whose
    files are gone (e.g. generated before durable storage) from the catalogue."""
    if not name:
        return False
    if (_out_dir() / os.path.basename(name)).exists():
        return True
    client, bucket = _r2()
    if not client:
        return False
    try:
        client.head_object(Bucket=bucket, Key=f"videos/{os.path.basename(name)}")
        return True
    except Exception:
        return False


class VideoIn(BaseModel):
    question: str
    answer: str
    title: str | None = None
    session_id: str | None = None      # optional: link the video to a saved Q&A session


@dataclass
class _Job:
    status: str = "running"      # running | done | error
    filename: str = ""
    title: str = ""
    duration_sec: float = 0.0
    error: str = ""


# In-process job registry. Fine for a single-node add-on; a multi-node deployment would
# back this with a table, but the add-on is deliberately lightweight and node-local.
_JOBS: dict[str, _Job] = {}


async def _run_bridge(job_id: str, payload: dict, session_id: str | None = None,
                      attach: AttachVideo | None = None) -> None:
    job = _JOBS[job_id]
    vdir, tsx = _video_dir(), _tsx()
    if not (vdir / "bin" / "answer-video.ts").exists():
        job.status, job.error = "error", f"video generator not found at {vdir}/bin/answer-video.ts"
        return
    try:
        # Force the generator to write into <video_dir>/out so the file endpoint can serve it.
        env = {**os.environ, "ROSTER_VIDEO_OUT_DIR": str(_out_dir())}
        proc = await asyncio.create_subprocess_exec(
            tsx, "bin/answer-video.ts",
            cwd=str(vdir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(json.dumps(payload).encode())
        if proc.returncode != 0:
            job.status = "error"
            job.error = (err.decode(errors="replace") or "bridge failed").strip()[-800:]
            return
        # Result is the LAST non-empty stdout line (JSON).
        lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip()]
        result = json.loads(lines[-1])
        job.status = "done"
        job.filename = os.path.basename(result["filename"])   # basename only — no path traversal
        job.title = result.get("title", "")
        job.duration_sec = float(result.get("durationSec", 0) or 0)
        # Mirror to R2 so the video survives container redeploys (best-effort).
        try:
            await asyncio.to_thread(_r2_put_video, job.filename)
        except Exception:
            pass
        # Best-effort: link the finished video to its saved Q&A (never fails the job).
        if session_id and attach is not None:
            try:
                await attach(session_id, filename=job.filename, title=job.title,
                             duration=job.duration_sec)
            except Exception:
                pass
    except Exception as e:   # noqa: BLE001 — surface any failure to the poller
        job.status, job.error = "error", str(e)[-800:]


# Strip structured-answer highlight markers ([[F]]…[[/F]] etc.) so they never leak into
# the video narration/storyboard. Content between markers is kept; only the tags are removed.
_HL_MARKERS = re.compile(r"\[\[/?[FRK]\]\]")


def build_video_router(attach_video: AttachVideo | None = None) -> APIRouter:
    router = APIRouter(prefix="/video", tags=["video"])

    @router.post("/generate")
    async def generate(body: VideoIn) -> dict:
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = _Job(title=body.title or body.question[:80])
        clean_answer = _HL_MARKERS.sub("", body.answer)
        payload = {"question": body.question, "answer": clean_answer,
                   "title": body.title or body.question[:80]}
        asyncio.create_task(_run_bridge(job_id, payload, body.session_id, attach_video))
        return {"job_id": job_id, "status": "running"}

    @router.get("/status/{job_id}")
    def status(job_id: str) -> dict:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return {"status": job.status, "filename": job.filename, "title": job.title,
                "duration_sec": job.duration_sec, "error": job.error}

    @router.get("/file/{filename}")
    def file(filename: str):
        # Basename-only + fixed dir → no traversal; must be an .mp4 we produced.
        name = os.path.basename(filename)
        if not name.endswith(".mp4"):
            raise HTTPException(status_code=400, detail="bad filename")
        path = _out_dir() / name
        if path.exists():                                   # fresh, on this container
            return FileResponse(str(path), media_type="video/mp4", filename=name)
        data = _r2_get_video(name)                          # durable copy (survives redeploys)
        if data is not None:
            return Response(content=data, media_type="video/mp4",
                            headers={"Content-Disposition": f'inline; filename="{name}"'})
        raise HTTPException(status_code=404, detail="video not found")

    return router
