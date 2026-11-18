"""File locks (flock) so a job or lock group never overlaps, across processes."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockHeld(Exception):
    """Another holder has one of the locks."""

    def __init__(self, name: str) -> None:
        super().__init__(f"lock held: {name}")
        self.name = name


@contextmanager
def hold(paths: list[Path]) -> Iterator[None]:
    """Take every lock without waiting. Raises LockHeld on the first busy one."""
    fds: list[int] = []
    try:
        for path in paths:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise LockHeld(path.stem) from None
            fds.append(fd)
        yield
    finally:
        for fd in reversed(fds):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
