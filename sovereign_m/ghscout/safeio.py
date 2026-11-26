"""New files only: O_CREAT|O_EXCL|O_NOFOLLOW, -vN suffix on collision. Never overwrites."""

from __future__ import annotations

import os
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
