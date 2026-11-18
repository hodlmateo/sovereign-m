"""ResponseCache: sqlite-backed, TTL + LRU, thread-safe, stats persisted."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

_WS = re.compile(r"\s+")

# Params that change the answer. "stream" is deliberately absent.
PARAM_KEYS = (
    "temperature", "top_p", "top_k", "min_p", "max_tokens", "max_completion_tokens",
    "num_predict", "num_ctx", "seed", "stop", "system", "response_format", "format",
    "frequency_penalty", "presence_penalty", "repeat_penalty", "n", "logit_bias",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  key TEXT PRIMARY KEY,
  body BLOB NOT NULL,
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  created REAL NOT NULL,
  expires REAL NOT NULL,
  last_access REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_lru ON entries(last_access);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def normalize_text(text: Any) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", str(text))).strip()


def _canon(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def params_hash(request: dict) -> str:
    params = {k: _canon(request[k]) for k in PARAM_KEYS if k in request}
    options = request.get("options")
    if isinstance(options, dict):
        params["options"] = _canon(options)
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_key(request: dict, route: str = "") -> str:
    """sha256 of canonical JSON of {route, model, prompt/messages, params hash}."""
    messages = request.get("messages")
    if isinstance(messages, list):
        prompt: Any = []
        for m in messages:
            if isinstance(m, dict):
                prompt.append({"role": normalize_text(m.get("role", "")), "content": _canon(m.get("content", ""))})
            else:
                prompt.append({"role": "", "content": _canon(m)})
    else:
        prompt = _canon(request.get("prompt", request.get("input", "")))
    doc = {
        "route": route,
        "model": normalize_text(request.get("model", "")),
        "prompt": prompt,
        "params": params_hash(request),
    }
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(
        self,
        path: str | Path,
        ttl_seconds: float = 86400,
        max_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 5000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ttl = float(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self.max_entries = int(max_entries)
        self.clock = clock
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        self._secure_sidecars()

    def _secure_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                try:
                    os.chmod(side, 0o600)
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "ResponseCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- counters --------------------------------------------------------
    def _bump(self, name: str, by: int = 1) -> None:
        self._db.execute(
            "INSERT INTO counters(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, by),
        )

    def record_bypass(self, reason: str) -> None:
        with self._lock:
            self._bump("bypass:" + reason)

    # -- core ------------------------------------------------------------
    def get(self, key: str) -> tuple[bytes, str] | None:
        with self._lock:
            now = self.clock()
            row = self._db.execute(
                "SELECT body, content_type, expires FROM entries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                self._bump("misses")
                return None
            if row[2] <= now:
                self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
                self._bump("expired")
                self._bump("misses")
                return None
            self._db.execute("UPDATE entries SET last_access = ? WHERE key = ?", (now, key))
            self._bump("hits")
            return bytes(row[0]), row[1]

    def put(self, key: str, body: bytes, content_type: str = "application/json", ttl: float | None = None) -> bool:
        body = bytes(body)
        if len(body) > self.max_bytes:
            return False
        now = self.clock()
        life = self.ttl if ttl is None else float(ttl)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries(key, body, content_type, size, created, expires, last_access) "
                "VALUES(?,?,?,?,?,?,?)",
                (key, body, content_type, len(body), now, now + life, now),
            )
            self._evict(protect=key)
            self._secure_sidecars()
        return True

    def _evict(self, protect: str | None = None) -> None:
        count, total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM entries").fetchone()
        if count <= self.max_entries and total <= self.max_bytes:
            return
        rows = self._db.execute("SELECT key, size FROM entries ORDER BY last_access ASC, rowid ASC").fetchall()
        evicted = 0
        for key, size in rows:
            if count <= self.max_entries and total <= self.max_bytes:
                break
            if key == protect:
                continue
            self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
            count -= 1
            total -= size
            evicted += 1
        if evicted:
            self._bump("evictions", evicted)

    def invalidate(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
            return cur.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM entries")
            return cur.rowcount

    def purge_expired(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM entries WHERE expires <= ?", (self.clock(),))
            if cur.rowcount:
                self._bump("expired", cur.rowcount)
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._db.execute("SELECT name, value FROM counters").fetchall())
            entries, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM entries").fetchone()
        bypasses = {k[7:]: v for k, v in counters.items() if k.startswith("bypass:")}
        hits = counters.get("hits", 0)
        misses = counters.get("misses", 0)
        lookups = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "bypasses": bypasses,
            "bypass_total": sum(bypasses.values()),
            "evictions": counters.get("evictions", 0),
            "expired": counters.get("expired", 0),
            "entries": entries,
            "bytes": size,
            "hit_rate": (hits / lookups) if lookups else 0.0,
        }


def format_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"hits        {stats['hits']}",
        f"misses      {stats['misses']}",
        f"hit rate    {stats['hit_rate'] * 100:.1f}%",
        f"bypasses    {stats['bypass_total']}",
    ]
    for reason, n in sorted(stats["bypasses"].items()):
        lines.append(f"  {reason:<22}{n}")
    lines += [
        f"evictions   {stats['evictions']}",
        f"expired     {stats['expired']}",
        f"entries     {stats['entries']}",
        f"bytes       {stats['bytes']}",
    ]
    return "\n".join(lines)
