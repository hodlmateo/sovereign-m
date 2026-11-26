"""Shared, safe board I/O for the sovereign loop (handoff, checker, runner).

Rules enforced here, once:
* tasks.jsonl is only ever opened read-only. Nothing in this package edits it.
* New files are created with O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW. On a name
  collision the next free ``-vN`` suffix is used. Nothing is overwritten.
* Secrets are redacted before text is written into a handoff or note.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

MAX_VERSIONS = 99
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def default_board() -> Path:
    override = os.environ.get("M_ROUNDTABLE_BOARD", "").strip()
    return Path(override).expanduser() if override else Path.home() / "m_hub" / "board"


def safe_id(text: str) -> str:
    cleaned = re.sub(r"\.{2,}", "_", SAFE_ID.sub("_", str(text))).lstrip(".")[:64]
    return cleaned or "card"


def read_cards(board: Path) -> list[dict]:
    """Latest row per id, in file order. Read-only; bad lines are skipped."""
    latest: dict[str, dict] = {}
    try:
        handle = open(board / "tasks.jsonl", "r", encoding="utf-8")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("id"):
                latest[str(obj["id"])] = obj
    return list(latest.values())


def open_cards(board: Path) -> list[dict]:
    return [c for c in read_cards(board) if str(c.get("status", "")).lower() != "done"]


def versioned(name: str, n: int) -> str:
    """``a.md`` -> ``a-v2.md`` (n>=2); n==1 keeps the name."""
    if n == 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name}-v{n}"
    return f"{stem}-v{n}.{ext}"


def _flags() -> int:
    return os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)


def write_new(directory: Path, name: str, text: str) -> Path:
    """Create ``directory/name`` as a NEW file, or ``-vN`` if taken. Never overwrites."""
    return _write(directory, name, text, reuse_same=False)[0]


def write_new_unless_same(directory: Path, name: str, text: str) -> tuple[Path, bool]:
    """Like write_new, but if an existing ``-vN`` already holds identical text, reuse it.

    Returns ``(path, created)``. Makes regenerating the same handoff every
    30 minutes a no-op instead of a pile of -vN copies.
    """
    return _write(directory, name, text, reuse_same=True)


def _write(directory: Path, name: str, text: str, reuse_same: bool) -> tuple[Path, bool]:
    if "/" in name or name.startswith(".") or ".." in name:
        raise ValueError(f"unsafe file name: {name!r}")
    directory.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    for n in range(1, MAX_VERSIONS + 1):
        target = directory / versioned(name, n)
        try:
            fd = os.open(target, _flags(), 0o644)
        except FileExistsError:
            try:
                if reuse_same and not target.is_symlink() and target.read_bytes() == data:
                    return target, False
            except OSError:
                pass
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return target, True
    raise OSError(f"no free name for {name} (tried up to -v{MAX_VERSIONS})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S{6,}))"
)


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)
