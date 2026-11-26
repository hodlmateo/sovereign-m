"""Safe writes. New files only (O_CREAT|O_EXCL|O_NOFOLLOW, -vN on collision) or an append-only log."""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_VERSIONS = 99


def versioned(name: str, n: int) -> str:
    if n == 1:
        return name
    stem, dot, ext = name.rpartition(".")
    return f"{name}-v{n}" if not dot or not stem else f"{stem}-v{n}.{ext}"


def write_new(directory: Path, name: str, text: str) -> Path:
    if "/" in name or name.startswith(".") or ".." in name:
        raise ValueError(f"unsafe file name: {name!r}")
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1, MAX_VERSIONS + 1):
        target = directory / versioned(name, n)
        try:
            fd = os.open(target, flags, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return target
    raise OSError(f"no free name for {name}")


def mkdir_new(parent: Path, name: str) -> Path:
    """Create a brand-new directory; if the name is taken use name-v2, -v3 ... Never reuses one."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, MAX_VERSIONS + 1):
        target = parent / (name if n == 1 else f"{name}-v{n}")
        try:
            os.mkdir(target, 0o755)
            return target
        except FileExistsError:
            continue
    raise OSError(f"no free directory name for {name}")


def append_line(path: Path, line: str, header: str = "") -> None:
    """Append one line (one write call, O_APPEND). The header goes in first only if the file is new/empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        data = (header if header and os.fstat(fd).st_size == 0 else "") + line.rstrip("\n") + "\n"
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return s or "idea"
