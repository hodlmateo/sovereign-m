"""Per-client keys. Only a salted hash is stored; the key is shown once."""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_KEY_LEN = 512


class KeyFileError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    client_id: str
    scopes: tuple[str, ...]
    rate_per_min: int | None

    def allows(self, route: str) -> bool:
        return "*" in self.scopes or route in self.scopes


def _digest(salt: bytes, key: bytes) -> bytes:
    return hashlib.sha256(salt + key).digest()


class KeyStore:
    """A JSON file of ``client -> {salt, sha256, scopes, rate_per_min, created}``.

    The file must be owned by this user and closed to group and others (0600).
    It is re-read when it changes, so ``keys revoke`` takes effect without a restart.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._records: dict[str, dict] = {}
        self.last_error = ""

    # ---- reading -------------------------------------------------------

    def _read_file(self) -> tuple[dict[str, dict], tuple[int, int] | None]:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return {}, None
        try:
            info = os.fstat(fd)
            if not _is_regular(info.st_mode):
                raise KeyFileError(f"keys file is not a regular file: {self.path}")
            if info.st_mode & 0o077:
                raise KeyFileError(
                    f"keys file {self.path} is readable by group or others "
                    f"(mode {info.st_mode & 0o777:o}); run: chmod 600 on it"
                )
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise KeyFileError(f"keys file {self.path} is owned by another user")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise KeyFileError(f"keys file is not valid JSON: {exc.msg}") from None
        finally:
            if fd >= 0:
                os.close(fd)
        clients = data.get("clients") if isinstance(data, dict) else None
        if not isinstance(clients, dict):
            raise KeyFileError("keys file has no clients mapping")
        return clients, (info.st_mtime_ns, info.st_size)

    def load(self) -> None:
        """Strict load for startup: raises KeyFileError on any problem."""
        with self._lock:
            self._records, self._stamp = self._read_file()
            self.last_error = ""

    def _refresh(self) -> dict[str, dict]:
        with self._lock:
            try:
                st = os.stat(self.path)
                stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                stamp = None
            if stamp == self._stamp and not self.last_error:
                return self._records
            try:
                self._records, self._stamp = self._read_file()
                self.last_error = ""
            except KeyFileError as exc:
                # Fail closed: a bad file means nobody gets in.
                self._records, self._stamp = {}, stamp
                self.last_error = str(exc)
            return self._records

    # ---- checking ------------------------------------------------------

    def authenticate(self, presented: str) -> Client | None:
        if not presented or len(presented) > MAX_KEY_LEN:
            return None
        data = presented.encode("utf-8", "ignore")
        found: str | None = None
        records = self._refresh()
        for name, rec in records.items():
            try:
                ok = hmac.compare_digest(
                    _digest(bytes.fromhex(rec["salt"]), data), bytes.fromhex(rec["sha256"])
                )
            except (KeyError, ValueError, TypeError):
                continue
            if ok:
                found = name
        if found is None:
            return None
        return _client(found, records[found])

    # ---- managing ------------------------------------------------------

    def add(self, client: str, scopes: list[str] | None = None, rate_per_min: int | None = None) -> str:
        """Create a client and return the new key. The key is not stored."""
        if not CLIENT_NAME.match(client):
            raise KeyFileError("client name: letters, digits, dot, dash, underscore (max 64)")
        clean = [s.strip() for s in (scopes or ["*"]) if s.strip()] or ["*"]
        if rate_per_min is not None and rate_per_min < 0:
            raise KeyFileError("rate must be zero or more")
        key = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        with self._lock:
            records, _ = self._read_file()
            if client in records:
                raise KeyFileError(f"client already exists: {client} (revoke it first to rotate)")
            records[client] = {
                "salt": salt.hex(),
                "sha256": _digest(salt, key.encode()).hex(),
                "scopes": clean,
                "rate_per_min": rate_per_min,
                "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            }
            self._write(records)
        return key

    def revoke(self, client: str) -> bool:
        with self._lock:
            records, _ = self._read_file()
            if client not in records:
                return False
            del records[client]
            self._write(records)
        return True

    def list(self) -> list[dict]:
        with self._lock:
            records, _ = self._read_file()
        return [
            {
                "client": name,
                "scopes": list(rec.get("scopes") or []),
                "rate_per_min": rec.get("rate_per_min"),
                "created": rec.get("created", ""),
            }
            for name, rec in sorted(records.items())
        ]

    def _write(self, records: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "clients": records}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _client(name: str, rec: dict) -> Client:
    rate = rec.get("rate_per_min")
    return Client(
        client_id=name,
        scopes=tuple(str(s) for s in rec.get("scopes") or ["*"]),
        rate_per_min=int(rate) if isinstance(rate, int) else None,
    )


def _is_regular(mode: int) -> bool:
    import stat

    return stat.S_ISREG(mode)


def parse_rate(text: str) -> int:
    """``60`` or ``60/min`` -> 60."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:/\s*min(?:ute)?)?\s*", text)
    if not match:
        raise ValueError("rate looks like 60 or 60/min")
    return int(match.group(1))
