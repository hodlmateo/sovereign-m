"""Run jobs: subprocess with timeout, retries with backoff, flock-based exclusion."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sovereign_m.scheduler import locks, schedule as sched
from sovereign_m.scheduler.config import Job, Settings
from sovereign_m.scheduler.redact import tail_redacted
from sovereign_m.scheduler.store import Store

MAX_BACKOFF = 3600.0


@dataclass
class Attempt:
    job: str
    run_id: str
    attempt: int
    start: float
    end: float
    exit_code: int | None
    status: str
    excerpt: str


def build_env(job: Job, environ: dict[str, str]) -> dict[str, str]:
    """A minimal environment: PATH, plus only the variables the job allowlists."""
    env = {"PATH": environ.get("PATH", os.defpath), "SOVEREIGN_M_JOB": job.name}
    for name in job.env_allowlist:
        if name in environ:
            env[name] = environ[name]
    return env


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def execute(job: Job, cwd: Path, env: dict[str, str], tail_bytes: int = 4096,
            grace: float = 2.0) -> tuple[int | None, str, str]:
    """Run one attempt. Returns (exit_code, status, redacted excerpt)."""
    argv: list[str] | str = job.command
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                argv, shell=job.shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            return 127, "failed", f"could not start: {exc.__class__.__name__}: {exc.strerror or exc}"
        timed_out = False
        try:
            proc.wait(timeout=job.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        # Always clear the whole group so a lingering grandchild cannot outlive the job.
        _kill_group(proc.pid, signal.SIGKILL)
        proc.wait()
        out.seek(0, os.SEEK_END)
        size = out.tell()
        out.seek(max(0, size - 4 * tail_bytes))
        excerpt = tail_redacted(out.read(), tail_bytes)
    if timed_out:
        return None, "timeout", excerpt
    code = proc.returncode
    return code, "ok" if code == 0 else "failed", excerpt


class Runner:
    def __init__(self, settings: Settings, store: Store | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 environ: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.clock = clock
        self.sleep = sleep
        self.environ = dict(os.environ if environ is None else environ)
        self._active: set[str] = set()
        self._guard = threading.Lock()

    # -- locks ------------------------------------------------------------
    def lock_paths(self, job: Job) -> list[Path]:
        paths = [self.settings.lock_dir / f"job-{job.name}.lock"]
        if job.lock_group:
            paths.append(self.settings.lock_dir / f"group-{job.lock_group}.lock")
        return paths

    # -- one job, with retries ---------------------------------------------
    def run_job(self, job: Job) -> list[Attempt]:
        run_id = uuid.uuid4().hex[:12]
        attempts: list[Attempt] = []
        try:
            with locks.hold(self.lock_paths(job)):
                self.store.mark_orphans(job.name, self.clock())
                cwd = self._cwd(job)
                env = build_env(job, self.environ)
                cfg = self.settings.cfg
                for number in range(1, job.retries + 2):
                    start = self.clock()
                    row = self.store.start(job.name, run_id, number, start, os.getpid())
                    code, status, excerpt = execute(
                        job, cwd, env, int(cfg["tail_bytes"]), float(cfg["kill_grace_seconds"]))
                    end = self.clock()
                    self.store.finish(row, end, code, status, excerpt)
                    attempts.append(Attempt(job.name, run_id, number, start, end, code, status, excerpt))
                    if status == "ok" or number > job.retries:
                        break
                    self.sleep(min(job.retry_backoff_seconds * 2 ** (number - 1), MAX_BACKOFF))
        except locks.LockHeld as exc:
            now = self.clock()
            note = f"skipped: lock {exc.name} is held by another run"
            self.store.record(job.name, run_id, 1, now, now, None, "skipped-locked", note)
            attempts.append(Attempt(job.name, run_id, 1, now, now, None, "skipped-locked", note))
        return attempts

    def _cwd(self, job: Job) -> Path:
        if job.working_dir:
            return Path(job.working_dir)
        path = self.settings.workspace / "scheduler" / "work"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    # -- scheduling ---------------------------------------------------------
    def last_start(self, job: Job) -> datetime | None:
        value = self.store.last_run_start(job.name)
        return datetime.fromtimestamp(value) if value is not None else None

    def due_jobs(self) -> list[Job]:
        now = datetime.fromtimestamp(self.clock())
        return [j for j in self.settings.jobs if j.enabled and sched.is_due(j.schedule, self.last_start(j), now)]

    def _spawn(self, job: Job) -> threading.Thread | None:
        with self._guard:
            if job.name in self._active:
                return None
            self._active.add(job.name)

        def work() -> None:
            try:
                self.run_job(job)
            finally:
                with self._guard:
                    self._active.discard(job.name)

        thread = threading.Thread(target=work, name=f"job-{job.name}")
        thread.start()
        return thread

    def run_once(self) -> list[str]:
        """One pass: start every due job (in parallel), wait for them, return their names."""
        threads = []
        names = []
        for job in self.due_jobs():
            thread = self._spawn(job)
            if thread:
                threads.append(thread)
                names.append(job.name)
        for thread in threads:
            thread.join()
        return names

    def loop(self, stop: threading.Event, tick: float | None = None) -> None:
        tick = float(self.settings.cfg["tick_seconds"]) if tick is None else tick
        threads: list[threading.Thread] = []
        while not stop.is_set():
            for job in self.due_jobs():
                thread = self._spawn(job)
                if thread:
                    threads.append(thread)
            threads = [t for t in threads if t.is_alive()]
            stop.wait(tick)
        for thread in threads:
            thread.join()
