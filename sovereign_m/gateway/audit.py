"""JSONL audit log. Fixed fields only: never a body, never a header, never a key."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qsl

_METHOD = re.compile(r"[^A-Za-z]")
MAX_PATH = 512


def clean_path(target: str, mode: str = "strip") -> str:
    """Drop the query string, or keep names and hide values."""
    path, _, query = target.partition("?")
    path = path.partition("#")[0]
    if mode == "redact" and query:
        names = [name for name, _ in parse_qsl(query, keep_blank_values=True)]
        path += "?" + "&".join(f"{name[:40]}=***" for name in names[:20])
    return path[:MAX_PATH]


class AuditLog:
    def __init__(self, path: Path | str | None, max_bytes: int = 5_242_880, keep: int = 5,
                 query: str = "strip", enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        self.query = query
        self.enabled = enabled and self.path is not None
        self._lock = threading.Lock()
        self._fd = -1
        self._size = 0

    def _open(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(self._fd, 0o600)
        self._size = os.fstat(self._fd).st_size

    def _rotate(self) -> None:
        assert self.path is not None
        os.close(self._fd)
        self._fd = -1
        oldest = self.path.with_name(f"{self.path.name}.{self.keep}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.keep - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{index}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        self._open()

    def log(self, *, client_id: str, route: str, method: str, target: str, status: int,
            bytes_in: int, bytes_out: int, duration_ms: float, cache: str | None = None) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
            "client_id": str(client_id)[:64],
            "route": str(route)[:64],
            "method": _METHOD.sub("", method)[:16],
            "path": clean_path(target, self.query),
            "status": int(status),
            "bytes_in": int(bytes_in),
            "bytes_out": int(bytes_out),
            "duration_ms": round(float(duration_ms), 1),
        }
        if cache:
            entry["cache"] = str(cache)[:32]
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            try:
                if self._fd < 0:
                    self._open()
                if self._size and self._size + len(line) > self.max_bytes:
                    self._rotate()
                os.write(self._fd, line)
                self._size += len(line)
            except OSError:
                pass  # logging must never take the proxy down

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
