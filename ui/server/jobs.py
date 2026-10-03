"""Run the fork's extension scripts as subprocesses and keep their output for streaming.

Each script runs exactly as on the command line, with this interpreter:
``python -m extensions.scripts.<name> ...`` from the repo root. A subprocess
keeps alpaca-py and LangGraph out of the server process, and a crash in a run
cannot take the UI down. Only the scripts in SCRIPTS can be started, and every
argument is built by the server from validated fields.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ui.server.paths import REPO_ROOT

# Every script the UI may start. None of them places an order.
SCRIPTS = frozenset({
    "fetch_bars",
    "run_upward_trend_scan",
    "run_breakdown_scan",
    "run_regime_bot",
    "run_router",
    "run_side_bot",
    "run_up_bot",
    "run_down_bot",
    "run_research",
})

# Exit codes shared by the extension scripts.
EXIT_MEANINGS = {0: "ok", 1: "failed", 2: "bad input", 3: "regime mismatch: bot blocked"}

# Jobs in the same group may not run at the same time (one research run, one pipeline).
EXCLUSIVE_GROUPS = frozenset({"research", "pipeline"})

MAX_JOBS_KEPT = 100


class JobConflict(RuntimeError):
    """A job of the same exclusive group is already running."""


@dataclass
class Step:
    script: str
    args: list[str]
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.script not in SCRIPTS:
            raise ValueError(f"script not allowed: {self.script}")

    @property
    def command(self) -> list[str]:
        return [sys.executable, "-m", f"extensions.scripts.{self.script}", *self.args]


@dataclass
class Job:
    id: str
    kind: str
    group: str | None
    steps: list[Step]
    status: str = "queued"  # queued, running, ok, failed, blocked
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    @property
    def done(self) -> bool:
        return self.status not in ("queued", "running")

    def summary(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "exit_code": self.exit_code,
            "exit_meaning": EXIT_MEANINGS.get(self.exit_code) if self.exit_code is not None else None,
            "steps": [{"script": s.script, "args": s.args, "exit_code": s.exit_code} for s in self.steps],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, steps: list[Step], group: str | None = None) -> Job:
        """Run the steps in order on a background thread; a non-zero exit stops the chain."""
        with self._lock:
            if group in EXCLUSIVE_GROUPS and any(j.group == group and not j.done for j in self._jobs.values()):
                raise JobConflict(f"a {group} job is already running")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, group=group, steps=steps)
            self._jobs[job.id] = job
            self._prune()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.done), key=lambda j: j.created_at)
        for job in finished[: max(0, len(self._jobs) - MAX_JOBS_KEPT)]:
            del self._jobs[job.id]

    def _run(self, job: Job) -> None:
        job.status = "running"
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        code = 0
        for step in job.steps:
            job.lines.append(f"$ python -m extensions.scripts.{step.script} {' '.join(step.args)}")
            try:
                proc = subprocess.Popen(
                    step.command, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    job.lines.append(line.rstrip("\n"))
                code = proc.wait()
            except OSError as exc:
                job.lines.append(f"error: could not start: {exc}")
                code = 1
            step.exit_code = code
            job.lines.append(f"[exit {code}: {EXIT_MEANINGS.get(code, 'error')}]")
            if code != 0:
                break
        job.exit_code = code
        job.status = {0: "ok", 3: "blocked"}.get(code, "failed")
        job.finished_at = datetime.now(UTC).isoformat()


runner = JobRunner()


def wait_for(job: Job, timeout: float = 30.0) -> Job:
    """Block until a job finishes (used by tests)."""
    deadline = time.monotonic() + timeout
    while not job.done and time.monotonic() < deadline:
        time.sleep(0.05)
    return job
