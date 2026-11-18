"""Attempt history in SQLite. The file is created with mode 0600."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

STATUSES = ("ok", "failed", "timeout", "skipped-locked", "running")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL,
    exit_code INTEGER,
    status TEXT NOT NULL,
    pid INTEGER,
    excerpt TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts (job, start);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        conn = self._conn()
        try:
            with conn:
                conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _exec(self, sql: str, args: tuple = ()) -> tuple[int, int]:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(sql, args)
                return cur.lastrowid or 0, cur.rowcount
        finally:
            conn.close()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def start(self, job: str, run_id: str, attempt: int, start: float, pid: int) -> int:
        return self._exec(
            "INSERT INTO attempts (job, run_id, attempt, start, status, pid) VALUES (?,?,?,?,?,?)",
            (job, run_id, attempt, start, "running", pid),
        )[0]

    def finish(self, row_id: int, end: float, exit_code: int | None, status: str, excerpt: str) -> None:
        self._exec(
            "UPDATE attempts SET end=?, exit_code=?, status=?, excerpt=? WHERE id=?",
            (end, exit_code, status, excerpt, row_id),
        )

    def record(self, job: str, run_id: str, attempt: int, start: float, end: float,
               exit_code: int | None, status: str, excerpt: str, pid: int | None = None) -> int:
        return self._exec(
            "INSERT INTO attempts (job, run_id, attempt, start, end, exit_code, status, pid, excerpt)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (job, run_id, attempt, start, end, exit_code, status, pid, excerpt),
        )[0]

    def mark_orphans(self, job: str, now: float) -> int:
        """Call only while holding the job lock: any 'running' row is a dead runner's."""
        return self._exec(
            "UPDATE attempts SET status='failed', end=?, excerpt=? WHERE job=? AND status='running'",
            (now, "orphaned: the runner that started this attempt is gone", job),
        )[1]

    def last_run_start(self, job: str) -> float | None:
        rows = self._rows("SELECT MAX(start) AS s FROM attempts WHERE job=? AND attempt=1", (job,))
        return rows[0]["s"] if rows else None

    def last_finished(self, job: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT * FROM attempts WHERE job=? AND status!='running' ORDER BY start DESC, id DESC LIMIT 1", (job,)
        )
        return rows[0] if rows else None

    def running(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM attempts WHERE status='running' ORDER BY start")

    def recent(self, limit: int = 25, job: str | None = None) -> list[dict[str, Any]]:
        if job:
            return self._rows("SELECT * FROM attempts WHERE job=? ORDER BY start DESC, id DESC LIMIT ?", (job, limit))
        return self._rows("SELECT * FROM attempts ORDER BY start DESC, id DESC LIMIT ?", (limit,))
