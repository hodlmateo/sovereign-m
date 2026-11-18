"""Read-only view of systemd user timers.

Only ``systemctl --user`` with an allowlisted verb (list-timers, show, status,
list-units) and ``journalctl --user -u UNIT -n N --no-pager -o json`` are ever
run. Every command goes through ``guard`` first. This module never writes a
unit file and never starts, stops, enables, disables, or replaces anything.
"""

from __future__ import annotations

import calendar
import fnmatch
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sovereign_m.scheduler.redact import redact

ALLOWED_VERBS = frozenset({"list-timers", "show", "status", "list-units"})
ALLOWED_FLAGS = frozenset({"--all", "--output=json", "--no-pager", "--no-legend", "--full", "--plain", "-p"})
SHOW_PROPS = ("Result", "ExecMainStatus", "ActiveState", "SubState")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@*\-]*$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@\-]{0,200}$")
MAX_LOG_LINES = 200


class SystemdRefused(Exception):
    """The command is not on the read-only allowlist."""


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str], float], CommandResult]


def default_runner(argv: list[str], timeout: float) -> CommandResult:
    """Run an already-guarded command without a shell."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "timed out")
    except OSError as exc:
        return CommandResult(126, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def guard(argv: list[str]) -> list[str]:
    """Return argv if it is a read-only command, else raise SystemdRefused."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise SystemdRefused("empty command")
    tool = argv[0]
    if tool == "systemctl":
        if len(argv) < 3 or argv[1] != "--user":
            raise SystemdRefused("only 'systemctl --user VERB' is allowed")
        verb = argv[2]
        if verb not in ALLOWED_VERBS:
            raise SystemdRefused(f"systemctl verb {verb!r} is refused; read-only verbs only: "
                                 + ", ".join(sorted(ALLOWED_VERBS)))
        for arg in argv[3:]:
            if arg.startswith("-"):
                if arg not in ALLOWED_FLAGS:
                    raise SystemdRefused(f"systemctl option {arg!r} is refused")
            elif not _SAFE_ARG.match(arg):
                raise SystemdRefused(f"unsafe argument {arg!r}")
        return argv
    if tool == "journalctl":
        ok = (len(argv) == 9 and argv[1:3] == ["--user", "-u"] and _UNIT.match(argv[3])
              and argv[4] == "-n" and argv[5].isdigit() and 1 <= int(argv[5]) <= MAX_LOG_LINES
              and argv[6:] == ["--no-pager", "-o", "json"])
        if not ok:
            raise SystemdRefused("only 'journalctl --user -u UNIT -n N --no-pager -o json' is allowed")
        return argv
    raise SystemdRefused(f"{tool!r} is not an allowed program")


@dataclass
class ExternalTimer:
    unit: str
    activates: str | None = None
    next_run: float | None = None
    last_run: float | None = None
    result: str | None = None
    exit_status: str | None = None
    active_state: str | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class SystemdReader:
    def __init__(self, runner: Runner = default_runner, timeout: float = 15.0) -> None:
        self._runner = runner
        self.timeout = timeout

    def run(self, argv: list[str]) -> CommandResult:
        return self._runner(guard(list(argv)), self.timeout)

    def systemctl(self, verb: str, *args: str) -> CommandResult:
        return self.run(["systemctl", "--user", verb, *args])

    # -- timers ---------------------------------------------------------------
    def list_timers(self) -> list[ExternalTimer]:
        res = self.systemctl("list-timers", "--all", "--output=json")
        if res.returncode == 0:
            parsed = parse_timers_json(res.stdout)
            if parsed is not None:
                return parsed
        res = self.systemctl("list-timers", "--all", "--no-pager")
        if res.returncode != 0:
            raise RuntimeError(f"list-timers failed ({res.returncode}): {res.stderr.strip()[:200]}")
        return parse_timers_text(res.stdout)

    def show(self, unit: str) -> dict[str, str]:
        if not _UNIT.match(unit):
            raise SystemdRefused(f"unsafe unit name {unit!r}")
        args: list[str] = [unit]
        for prop in SHOW_PROPS:
            args += ["-p", prop]
        res = self.systemctl("show", *args)
        out: dict[str, str] = {}
        for line in res.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key] = value
        return out

    def journal(self, unit: str, lines: int) -> list[str]:
        lines = max(1, min(int(lines), MAX_LOG_LINES))
        res = self.run(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "json"])
        return parse_journal_json(res.stdout) if res.returncode == 0 else []


# -- parsers ---------------------------------------------------------------------

def _usec(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value) / 1_000_000
    return None


def parse_timers_json(text: str) -> list[ExternalTimer] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("unit"):
            continue
        out.append(ExternalTimer(
            unit=str(item["unit"]),
            activates=str(item["activates"]) if item.get("activates") else None,
            next_run=_usec(item.get("next")),
            last_run=_usec(item.get("last")),
        ))
    return out


_STAMP = r"(?:[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S+|n/a)"
_TEXT_ROW = re.compile(
    rf"^(?P<next>{_STAMP})\s+(?P<left>.*?)\s+(?P<last>{_STAMP})\s+(?P<passed>.*?)\s+"
    r"(?P<unit>\S+\.timer)\s+(?P<act>\S+)\s*$"
)
_STAMP_PARTS = re.compile(r"^[A-Za-z]{3} (\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) (\S+)$")


def _stamp(text: str) -> float | None:
    match = _STAMP_PARTS.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(x) for x in match.groups()[:6])
    fields = (year, month, day, hour, minute, second, 0, 0, -1)
    if match.group(7) in {"UTC", "GMT", "Z"}:
        return float(calendar.timegm(fields))
    return time.mktime(fields)  # other zone names are read as local time


def parse_timers_text(text: str) -> list[ExternalTimer]:
    out = []
    for line in text.splitlines():
        match = _TEXT_ROW.match(line.strip())
        if match:
            out.append(ExternalTimer(
                unit=match["unit"],
                activates=match["act"],
                next_run=_stamp(match["next"]),
                last_run=_stamp(match["last"]),
            ))
    return out


def parse_journal_json(text: str, limit: int = MAX_LOG_LINES) -> list[str]:
    lines = []
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("MESSAGE") if isinstance(entry, dict) else None
        if isinstance(message, list):  # binary messages arrive as byte arrays
            try:
                message = bytes(message).decode("utf-8", errors="replace")
            except (TypeError, ValueError):
                continue
        if isinstance(message, str):
            lines.append(redact(message.strip())[:500])
    return lines[-limit:]


# -- the import ------------------------------------------------------------------------

def import_timers(reader: SystemdReader, units: list[str] | None = None, max_units: int = 40,
                  log_lines: int = 5) -> tuple[list[ExternalTimer], str | None]:
    """Read timers, their service state, and a few redacted log lines. Never raises."""
    patterns = [str(u) for u in (units or ["*"])]
    try:
        timers = reader.list_timers()
    except (RuntimeError, SystemdRefused) as exc:
        return [], str(exc)
    chosen = [t for t in timers if any(fnmatch.fnmatch(t.unit, p) for p in patterns)][:max_units]
    for timer in chosen:
        service = timer.activates or re.sub(r"\.timer$", ".service", timer.unit)
        try:
            info = reader.show(service)
            timer.result = info.get("Result") or None
            timer.exit_status = info.get("ExecMainStatus") or None
            timer.active_state = info.get("ActiveState") or None
            if log_lines > 0:
                timer.logs = reader.journal(service, log_lines)
        except SystemdRefused as exc:
            timer.error = str(exc)
    return chosen, None
