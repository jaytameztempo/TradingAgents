"""Job status and live output, streamed as Server-Sent Events."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ui.server.jobs import runner

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job(job_id: str):
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job


@router.get("")
def list_jobs(limit: int = 20) -> list[dict]:
    return [j.summary() for j in runner.recent(max(1, min(limit, 100)))]


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    job = _job(job_id)
    return {**job.summary(), "lines": job.lines}


@router.get("/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """Each output line as an SSE "line" event, then one "done" event with the job summary."""
    job = _job(job_id)

    async def events():
        sent = 0
        while True:
            lines = job.lines
            while sent < len(lines):
                yield f"event: line\ndata: {json.dumps(lines[sent])}\n\n"
                sent += 1
            if job.done and sent >= len(job.lines):
                yield f"event: done\ndata: {json.dumps(job.summary())}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
