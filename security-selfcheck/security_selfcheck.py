#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Sovereign Security Self-Check — a defensive, read-only audit for a Linux box you own.

Pure Python 3 standard library. No pip packages, no root, no network calls,
and no changes to the system except the optional --purple-test,
which creates and deletes a temporary decoy under /tmp.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
TOOL_NAME = "security-selfcheck"

STATUS_ORDER = {
    "OK": 0,
    "INFO": 1,
    "SKIP": 2,
    "WARN": 3,
    "FAIL": 4,
    "ALERT": 5,
}

STATUS_COLORS = {
    "OK": "\033[32m",
    "INFO": "\033[36m",
    "SKIP": "\033[90m",
    "WARN": "\033[33m",
    "FAIL": "\033[31m",
    "ALERT": "\033[31;1m",
}
RESET = "\033[0m"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

FAILED_AUTH_RES = [
    re.compile(
        r"Failed (?:password|publickey|keyboard-interactive)\s+for\s+(?:invalid user\s+)?(?P<user>\S+)\s+from\s+(?P<src>\S+)",
        re.I,
    ),
    re.compile(r"Invalid user\s+(?P<user>\S+)\s+from\s+(?P<src>\S+)", re.I),
    re.compile(
        r"Connection closed by authenticating user\s+(?P<user>\S+)\s+(?P<src>\S+)",
        re.I,
    ),
    re.compile(r"authentication failure\b.*\buser=(?P<user>\S+)", re.I),
    re.compile(r"Failed keyboard-interactive[/ ]pam.+from\s+(?P<src>\S+)", re.I),
]

ROOT_SSH_RE = re.compile(
    r"Accepted\s+(?P<method>password|publickey|keyboard-interactive|otp)\s+for\s+root\s+from\s+(?P<src>\S+)",
    re.I,
)

SS_LINE_RE = re.compile(
    r"^(?P<netid>\S+)\s+(?P<state>\S+)\s+\S+\s+\S+\s+(?P<local>\S+)\s+(?P<peer>\S+)(?:\s+(?P<rest>.*))?$"
)

PROC_LISTEN = "0A"
MAX_LOG_BYTES = 8 * 1024 * 1024


@dataclass
class Finding:
    check_id: str
    title: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "check": self.check_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
        }
        if self.details:
            data["details"] = self.details
        if self.remediation:
            data["remediation"] = self.remediation
        return data


def colorize(status: str, enabled: bool) -> str:
    if not enabled:
        return f"[{status}]"
    return f"{STATUS_COLORS.get(status, '')}[{status}]{RESET}"


def worst_status(*statuses: str) -> str:
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(
    argv: list[str],
    timeout: int = 20,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    merged["DEBIAN_FRONTEND"] = "noninteractive"
    if env:
        merged.update(env)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def load_rules(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Rules file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Rules file is not valid JSON ({path}): {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Rules file must contain a JSON object: {path}")
    return data


def default_rules_path() -> Path:
    return Path(__file__).resolve().parent / "rules" / "default_rules.json"


def expand_user_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expanduser(value)).resolve()


def sha256_file(path: Path, max_bytes: int = 32 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = max_bytes
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days}d {hours}h" if hours else f"{days}d"


def parse_proc_address(hex_ip: str, hex_port: str) -> tuple[str, int]:
    port = int(hex_port, 16)
    if len(hex_ip) == 8:
        packed = struct.pack("<I", int(hex_ip, 16))
        return socket.inet_ntop(socket.AF_INET, packed), port
    parts = [hex_ip[i : i + 8] for i in range(0, 32, 8)]
    packed = b"".join(struct.pack("<I", int(part, 16)) for part in parts)
    return socket.inet_ntop(socket.AF_INET6, packed), port


def split_host_port(local: str) -> tuple[str, int] | None:
    if local.startswith("[") and "]:" in local:
        host, port_s = local[1:].split("]:", 1)
    elif local.count(":") == 1:
        host, port_s = local.split(":", 1)
    else:
        idx = local.rfind(":")
        if idx < 0:
            return None
        host, port_s = local[:idx], local[idx + 1 :]
    try:
        return host, int(port_s)
    except ValueError:
        return None


def ip_in_cidrs(addr: str, cidrs: list[str]) -> bool:
    text = addr.strip()
    if text.startswith("::ffff:"):
        maybe_v4 = text[7:]
        try:
            ipaddress.IPv4Address(maybe_v4)
            text = maybe_v4
        except ipaddress.AddressValueError:
            pass
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if ip.version == network.version and ip in network:
            return True
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and ":" not in cidr:
            try:
                if mapped in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
    return False


def bind_label(addr: str, cidrs: list[str], notes: dict[str, str] | None = None) -> str:
    notes = notes or {}
    text = addr.strip()
    if text in {"<WILDCARD_V4>", "*", "::", "::0"}:
        return "all interfaces"
    for cidr in cidrs:
        if ip_in_cidrs(text, [cidr]):
            return notes.get(cidr, cidr)
    return "LAN or public"


def read_text_tail(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            return handle.read()
    except (OSError, PermissionError):
        return ""


def parse_syslog_time(token: str, now: datetime) -> datetime | None:
    parts = token.split()
    if len(parts) < 3:
        return None
    month = MONTHS.get(parts[0][:3].title())
    try:
        day = int(parts[1])
        hh, mm, ss = (int(x) for x in parts[2].split(":")[:3])
    except ValueError:
        return None
    if not month:
        return None
    year = now.year
    try:
        parsed = datetime(year, month, day, hh, mm, ss, tzinfo=now.tzinfo)
    except ValueError:
        return None
    if parsed - now > timedelta(days=2):
        try:
            parsed = parsed.replace(year=year - 1)
        except ValueError:
            pass
    return parsed


def parse_iso_loose(value: str, now: datetime) -> datetime | None:
    text = value.strip().replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed
    except ValueError:
        return parse_syslog_time(text, now)



def _iter_auth_messages(lookback: datetime, now: datetime) -> tuple[list[tuple[datetime, str]], list[str]]:
    messages: list[tuple[datetime, str]] = []
    sources: list[str] = []

    log_paths = [
        Path("/var/log/auth.log"),
        Path("/var/log/auth.log.1"),
        Path("/var/log/secure"),
        Path("/var/log/secure.1"),
    ]
    for path in log_paths:
        if not path.exists():
            continue
        text = read_text_tail(path)
        if not text:
            if not os.access(path, os.R_OK):
                sources.append(
                    f"{path} exists but is not readable (normal without adm/systemd-journal group)"
                )
            continue
        sources.append(str(path))
        for line in text.splitlines():
            if len(line) < 15:
                continue
            stamp = parse_syslog_time(line[:15], now)
            if stamp is None or stamp < lookback:
                continue
            rest = line[16:]
            if " " in rest:
                rest = rest.split(" ", 1)[1]
            messages.append((stamp, rest))

    if which("journalctl"):
        argv = [
            "journalctl",
            "-q",
            "--no-pager",
            "--since",
            lookback.strftime("%Y-%m-%d %H:%M:%S"),
            "-o",
            "short-iso",
            "-t",
            "sshd",
            "-t",
            "sshd-session",
            "-t",
            "login",
        ]
        result = run_cmd(argv, timeout=25)
        if result.returncode == 0 and result.stdout.strip():
            sources.append("journalctl (sshd/login)")
            for line in result.stdout.splitlines():
                parts = line.split(maxsplit=3)
                if len(parts) < 4:
                    continue
                stamp = parse_iso_loose(parts[0], now)
                if stamp is None or stamp < lookback:
                    continue
                messages.append((stamp, parts[3]))
        elif result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip().splitlines()
            hint = err[0] if err else "journalctl was not readable"
            sources.append(f"journalctl skipped: {hint[:160]}")

    return messages, sources


def check_auth(rules: dict[str, Any]) -> Finding:
    title = "Auth / SSH log rules"
    auth = rules.get("auth") or {}
    lookback_hours = int(auth.get("lookback_hours", 24))
    threshold = int(auth.get("failed_burst_threshold", 8))
    window_min = int(auth.get("failed_burst_window_minutes", 10))
    alert_root = bool(auth.get("alert_on_root_ssh_success", True))

    now = datetime.now().astimezone()
    lookback = now - timedelta(hours=lookback_hours)
    messages, sources = _iter_auth_messages(lookback, now)

    readable = any(
        (not s.startswith("journalctl skipped") and "not readable" not in s)
        for s in sources
    )
    if not messages and not readable:
        hint = sources[0] if sources else "No auth log or journal was readable."
        return Finding(
            check_id="auth",
            title=title,
            status="SKIP",
            summary=(
                "Auth logs are not readable as this user, so sign-in burst and "
                "root-SSH rules were skipped. That is normal on many desktops."
            ),
            details=sources or [hint],
            remediation=(
                "Fix (about 1 minute):\n"
                "1. If you want this check, add your user to the 'adm' and/or "
                "'systemd-journal' group, then log out and back in.\n"
                "   sudo usermod -aG adm,systemd-journal $USER\n"
                "2. Or install a daily root timer that writes a sanitized "
                "summary (failed-login counts only) to a file your user can read.\n"
                "3. Re-run: python3 security_selfcheck.py\n"
                "Do not chmod 644 /var/log/auth.log on a shared machine."
            ),
        )

    failures: list[tuple[datetime, str, str]] = []
    root_ok: list[tuple[datetime, str, str]] = []
    for stamp, body in messages:
        root_hit = ROOT_SSH_RE.search(body)
        if root_hit:
            root_ok.append((stamp, root_hit.group("method"), root_hit.group("src")))
            continue
        for pattern in FAILED_AUTH_RES:
            hit = pattern.search(body)
            if not hit:
                continue
            user = hit.groupdict().get("user") or "unknown"
            src = hit.groupdict().get("src") or "unknown"
            failures.append((stamp, user, src))
            break

    failures.sort(key=lambda item: item[0])
    window = timedelta(minutes=window_min)
    left = 0
    max_count = 0
    max_slice: list[tuple[datetime, str, str]] = []
    for right, item in enumerate(failures):
        while failures[left][0] < item[0] - window:
            left += 1
        count = right - left + 1
        if count > max_count:
            max_count = count
            max_slice = failures[left : right + 1]

    bursts: list[str] = []
    if max_count >= threshold and max_slice:
        by_src: dict[str, int] = defaultdict(int)
        by_user: dict[str, int] = defaultdict(int)
        for _, user, src in max_slice:
            by_src[src] += 1
            by_user[user] += 1
        top_src = ", ".join(
            f"{src} ({n})" for src, n in sorted(by_src.items(), key=lambda kv: -kv[1])[:5]
        )
        top_user = ", ".join(
            f"{user} ({n})" for user, n in sorted(by_user.items(), key=lambda kv: -kv[1])[:5]
        )
        bursts.append(
            f"{max_count} failed sign-ins in {window_min} minutes "
            f"(threshold is {threshold}). Top sources: {top_src}. Accounts tried: {top_user}."
        )

    details = [f"Sources: {', '.join(sources)}"] if sources else []
    details.append(
        f"Lookback {lookback_hours}h: {len(failures)} failed sign-in events, "
        f"{len(root_ok)} successful root SSH logins."
    )

    status = "OK"
    summary_parts: list[str] = []
    remediations: list[str] = []

    if bursts:
        status = "WARN"
        summary_parts.extend(bursts)
        remediations.append(
            "Failed-login burst — fix (about 1 minute):\n"
            "1. Confirm you (or a known service) caused the failures. If not, "
            "the source address is probing your SSH port.\n"
            "2. Prefer SSH keys only: set PasswordAuthentication no in your "
            "sshd_config and reload ssh.\n"
            "3. Bind SSH to a mesh VPN address instead of every interface, "
            "or add a simple allowlist (firewall or sshd AllowUsers).\n"
            "4. Re-run this check after the window closes to confirm the burst stopped."
        )

    if alert_root and root_ok:
        status = worst_status(status, "WARN")
        methods = ", ".join(sorted({method for _, method, _ in root_ok}))
        summary_parts.append(
            f"{len(root_ok)} successful SSH login(s) for root in the last "
            f"{lookback_hours}h (method: {methods}). Root SSH is unexpected on most boxes."
        )
        remediations.append(
            "Root SSH login — fix (about 1 minute):\n"
            "1. If this was not you, rotate credentials and disable the compromised login method immediately.\n"
            "2. Set PermitRootLogin no in sshd_config and reload ssh. Use a normal user plus sudo instead.\n"
            "3. Keep a record of who is allowed to log in (AllowUsers / keys in <HOME>/.ssh/authorized_keys only)."
        )

    if not summary_parts:
        summary_parts.append(
            f"No sign-in bursts or unexpected root SSH logins in the last {lookback_hours} hours."
        )

    return Finding(
        check_id="auth",
        title=title,
        status=status,
        summary=" ".join(summary_parts),
        details=details,
        remediation="\n\n".join(remediations) if remediations else None,
    )



def _crontab_lines() -> tuple[list[str], str | None]:
    if not which("crontab"):
        return [], "crontab command is not installed"
    result = run_cmd(["crontab", "-l"], timeout=10)
    text = (result.stdout or "").strip()
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "no crontab" in err:
            return [], None
        msg = (result.stderr or result.stdout or "crontab -l failed").strip().splitlines()
        return [], (msg[0] if msg else "crontab -l failed")[:160]
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines, None


def _user_systemd_units() -> list[str]:
    """Inventory units the user actually installed, not every distro user unit."""
    found: list[str] = []
    seen: set[str] = set()
    base = Path.home() / ".config" / "systemd" / "user"
    home = str(Path.home())

    def add(label: str) -> None:
        if label not in seen:
            seen.add(label)
            found.append(label)

    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if path.suffix in {".service", ".timer", ".socket", ".path"} and path.is_file():
                add(path.relative_to(base).as_posix())

    if which("systemctl"):
        # Only scheduled timers whose unit file lives under $HOME (skip distro defaults).
        result = run_cmd(
            ["systemctl", "--user", "list-timers", "--no-pager", "--no-legend"],
            timeout=15,
        )
        if result.returncode == 0:
            for raw in result.stdout.splitlines():
                parts = raw.split()
                for name in (token for token in parts if token.endswith(".timer")):
                    if any(name in item for item in found):
                        continue
                    show = run_cmd(
                        ["systemctl", "--user", "show", name, "-p", "FragmentPath", "--value"],
                        timeout=8,
                    )
                    fragment = (show.stdout or "").strip()
                    if fragment.startswith(home):
                        add(f"timer:{name}")
    return found


def check_persistence(rules: dict[str, Any]) -> Finding:
    title = "Persistence (cron and user systemd)"
    allowlist = [str(x) for x in (rules.get("persistence") or {}).get("allowlist") or []]
    cron_lines, cron_err = _crontab_lines()
    units = _user_systemd_units()

    details: list[str] = []
    if cron_err and not cron_lines:
        details.append(f"crontab: {cron_err}")
    elif not cron_lines:
        details.append("User crontab: (empty)")
    else:
        details.append("User crontab:")
        details.extend(f"  - {line}" for line in cron_lines)

    if units:
        details.append("User systemd units / timers:")
        details.extend(f"  - {item}" for item in units)
    else:
        details.append("User systemd units / timers: (none found under <HOME>/.config/systemd/user)")

    inventory = [f"crontab:{line}" for line in cron_lines] + [f"systemd-user:{item}" for item in units]

    if allowlist:
        extras = [
            item
            for item in inventory
            if item not in allowlist and not any(item.endswith(allowed) or allowed in item for allowed in allowlist)
        ]
        if extras:
            noun = "y" if len(extras) == 1 else "ies"
            return Finding(
                check_id="persistence",
                title=title,
                status="WARN",
                summary=f"{len(extras)} persistence entr{noun} are not on your allowlist.",
                details=details + ["Not on allowlist:"] + [f"  - {item}" for item in extras],
                remediation=(
                    "Unexpected persistence — fix (about 1 minute):\n"
                    "1. Read each extra crontab line and each unit name below. "
                    "If you installed it, add it to persistence.allowlist in your rules file.\n"
                    "2. If you do not recognize it, disable it:\n"
                    "   crontab -e          (delete the unknown line)\n"
                    "   systemctl --user disable --now UNIT.timer\n"
                    "3. Re-run: python3 security_selfcheck.py"
                ),
            )

    count = len(cron_lines) + len(units)
    if count == 0:
        summary = "No user crontab entries or user systemd units/timers were found."
    else:
        summary = (
            f"Found {len(cron_lines)} crontab line(s) and {len(units)} user systemd "
            "unit/timer item(s). Review the list — anything you did not install is worth a closer look."
        )
    return Finding(
        check_id="persistence",
        title=title,
        status="OK",
        summary=summary,
        details=details,
    )



@dataclass
class Listener:
    proto: str
    addr: str
    port: int
    process: str
    source: str


def _wildcard_peer(peer: str) -> bool:
    text = (peer or "").strip()
    if text in {"", "*", "*:*", "<WILDCARD_V4>:*", "[::]:*", "[::0]:*", ":::*"}:
        return True
    return text.endswith(":*") and any(
        text.startswith(prefix) for prefix in ("<WILDCARD_V4>:", "[::]:", "[::0]:", "*:")
    )


def _parse_ss_process(rest: str) -> str:
    if not rest:
        return ""
    match = re.search(r'"([^"]+)"', rest)
    if match:
        return match.group(1)
    match = re.search(r"users:\(\(([^,]+)", rest)
    if match:
        return match.group(1).strip('"')
    return rest.strip()[:80]


def _listeners_from_ss(include_udp: bool) -> list[Listener] | None:
    if not which("ss"):
        return None
    result = run_cmd(["ss", "-tulpnH"], timeout=15)
    text = result.stdout or ""
    if result.returncode != 0 or not text.strip():
        result = run_cmd(["ss", "-tulpn"], timeout=15)
        text = result.stdout or ""
        if result.returncode != 0:
            return None
    listeners: list[Listener] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("netid") or line.lower().startswith("state"):
            continue
        match = SS_LINE_RE.match(line)
        if not match:
            continue
        netid = match.group("netid").lower()
        state = match.group("state").upper()
        if netid.startswith("tcp") and state not in {"LISTEN"}:
            continue
        if netid.startswith("udp") and not include_udp:
            continue
        if netid.startswith("udp") and not _wildcard_peer(match.group("peer") or ""):
            continue
        parsed = split_host_port(match.group("local"))
        if not parsed:
            continue
        addr, port = parsed
        listeners.append(
            Listener(
                proto=netid,
                addr=addr,
                port=port,
                process=_parse_ss_process(match.group("rest") or ""),
                source="ss",
            )
        )
    return listeners


def _listeners_from_proc(include_udp: bool) -> list[Listener]:
    table = [
        ("/proc/net/tcp", "tcp", True),
        ("/proc/net/tcp6", "tcp", True),
        ("/proc/net/udp", "udp", include_udp),
        ("/proc/net/udp6", "udp", include_udp),
    ]
    listeners: list[Listener] = []
    inode_to_proc: dict[str, str] = {}

    try:
        pids = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        pids = []
    for proc in pids:
        comm = ""
        try:
            comm = (proc / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inode_to_proc.setdefault(target[8:-1], comm or proc.name)
        except OSError:
            continue

    for path, proto, enabled in table:
        if not enabled:
            continue
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 10:
                continue
            local, rem, st = cols[1], cols[2], cols[3]
            if proto.startswith("tcp") and st != PROC_LISTEN:
                continue
            if proto.startswith("udp"):
                rem_ip, rem_port = rem.split(":")
                if int(rem_ip, 16) != 0 or int(rem_port, 16) != 0:
                    continue
            try:
                hex_ip, hex_port = local.split(":")
                addr, port = parse_proc_address(hex_ip, hex_port)
            except (ValueError, OSError):
                continue
            inode = cols[9]
            listeners.append(
                Listener(
                    proto=proto,
                    addr=addr,
                    port=port,
                    process=inode_to_proc.get(inode, ""),
                    source=path,
                )
            )
    return listeners


def check_listen(rules: dict[str, Any]) -> Finding:
    title = "Listening ports & exposure"
    conf = rules.get("listen") or {}
    cidrs = [str(x) for x in conf.get("safe_bind_cidrs") or ["<LOOPBACK_V4>", "::1/128", "<MESH_VPN_V4>"]]
    notes = {str(k): str(v) for k, v in (conf.get("safe_bind_notes") or {}).items()}
    allow_ports = {int(p) for p in (conf.get("exposed_ports_allowlist") or [])}
    include_udp = bool(conf.get("include_udp", True))

    listeners = _listeners_from_ss(include_udp)
    source = "ss"
    if listeners is None:
        listeners = _listeners_from_proc(include_udp)
        source = "/proc/net"

    uniq: dict[tuple[str, str, int], Listener] = {}
    for item in listeners:
        key = (item.proto, item.addr, item.port)
        if key not in uniq or (item.process and not uniq[key].process):
            uniq[key] = item
    listeners = list(uniq.values())

    exposed: list[Listener] = []
    safe: list[Listener] = []
    for item in listeners:
        addr = item.addr
        if addr in {"<WILDCARD_V4>", "*", "::", "::0", ""}:
            exposed.append(item)
        elif ip_in_cidrs(addr, cidrs):
            safe.append(item)
        else:
            exposed.append(item)

    unexpected = [item for item in exposed if item.port not in allow_ports]
    expected = [item for item in exposed if item.port in allow_ports]

    def fmt(item: Listener) -> str:
        proc = f" ({item.process})" if item.process else ""
        label = bind_label(item.addr, cidrs, notes)
        return f"{item.proto} {item.addr}:{item.port}{proc} — {label}"

    details = [f"Source: {source}. {len(listeners)} listening socket(s) found."]
    if safe:
        details.append("Bound only to loopback / mesh-VPN ranges (not flagged):")
        ordered = sorted(safe, key=lambda x: (x.proto, x.port, x.addr))
        details.extend(f"  - {fmt(item)}" for item in ordered[:40])
        if len(safe) > 40:
            details.append(f"  - … {len(safe) - 40} more")
    if expected:
        details.append("Non-loopback listeners on your allowlist:")
        details.extend(
            f"  - {fmt(item)}" for item in sorted(expected, key=lambda x: (x.proto, x.port, x.addr))
        )

    if unexpected:
        details.append("Non-loopback listeners not on your allowlist:")
        details.extend(
            f"  - {fmt(item)}" for item in sorted(unexpected, key=lambda x: (x.proto, x.port, x.addr))
        )
        ports = ", ".join(str(p) for p in sorted({item.port for item in unexpected}))
        return Finding(
            check_id="listen",
            title=title,
            status="WARN",
            summary=(
                f"{len(unexpected)} listener(s) are reachable beyond loopback / mesh-VPN "
                f"ranges and are not on your allowlist (ports: {ports})."
            ),
            details=details,
            remediation=(
                "Exposed listener — fix (about 1 minute):\n"
                "1. For each line below, ask: should this service be reachable from other machines?\n"
                "2. If no, bind it to <LOOPBACK_HOST> (or your mesh VPN address) in that service's config and restart it.\n"
                "3. If yes, add the port to listen.exposed_ports_allowlist in your rules file so the next run stays quiet.\n"
                "4. Prefer a host firewall (ufw allow from a trusted source) over binding to <WILDCARD_V4> on a workstation.\n"
                "5. Re-run: python3 security_selfcheck.py"
            ),
        )

    if expected:
        summary = (
            f"{len(expected)} non-loopback listener(s) match your allowlist. "
            "Everything else is on loopback or a mesh-VPN range."
        )
    else:
        summary = "Every listening socket is bound to loopback or a mesh-VPN range."
    return Finding(
        check_id="listen",
        title=title,
        status="OK",
        summary=summary,
        details=details,
    )



def _ufw_status() -> tuple[str, list[str], str | None]:
    details: list[str] = []
    conf_path = Path("/etc/ufw/ufw.conf")
    enabled_in_conf: bool | None = None
    if conf_path.is_file():
        try:
            text = conf_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.strip().upper().startswith("ENABLED="):
                    enabled_in_conf = line.split("=", 1)[1].strip().lower() in {"yes", "true", "1"}
        except OSError:
            pass

    if which("ufw"):
        result = run_cmd(["ufw", "status"], timeout=10)
        out = (result.stdout or result.stderr or "").strip()
        low = out.lower()
        if result.returncode == 0 and "status: active" in low:
            details.append("ufw reports: active.")
            return "OK", details, None
        if result.returncode == 0 and "status: inactive" in low:
            details.append("ufw is installed but inactive.")
            return (
                "WARN",
                details,
                "Firewall is off — fix (about 1 minute):\n"
                "1. Enable a host firewall. On Ubuntu/Debian:\n"
                "   sudo ufw default deny incoming\n"
                "   sudo ufw default allow outgoing\n"
                "   sudo ufw allow OpenSSH    # only if you need SSH\n"
                "   sudo ufw enable\n"
                "2. Re-run: python3 security_selfcheck.py",
            )
        if "need to be root" in low or "superuser" in low or result.returncode != 0:
            if enabled_in_conf is True:
                details.append(
                    "ufw is installed and /etc/ufw/ufw.conf says ENABLED=yes, "
                    "but 'ufw status' needs root so rules were not listed."
                )
                return "INFO", details, None
            if enabled_in_conf is False:
                details.append("ufw is installed, but /etc/ufw/ufw.conf says ENABLED=no.")
                return (
                    "WARN",
                    details,
                    "Firewall is off — fix (about 1 minute):\n"
                    "1. Enable it with: sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw enable\n"
                    "2. Allow only the services you actually need, then re-run this check.",
                )
            details.append("ufw is installed, but this user cannot read its status (root required).")
            return (
                "SKIP",
                details,
                "Firewall status needs root — fix (about 1 minute):\n"
                "1. Run: sudo ufw status verbose\n"
                "2. If it says inactive, enable it (deny incoming, allow outgoing, then allow only what you need).\n"
                "3. Optional: add a daily root timer that writes `ufw status` to a file your user can read.",
            )
    else:
        details.append("ufw is not installed.")
        other = [name for name in ("nft", "iptables", "firewall-cmd") if which(name)]
        if other:
            details.append("Other firewall tools found: " + ", ".join(other) + ".")
            return (
                "INFO",
                details,
                "ufw is not installed, but another firewall tool is. That can be fine — "
                "just confirm incoming traffic is default-deny. On Ubuntu, `sudo ufw enable` is the short path.",
            )
        return (
            "WARN",
            details,
            "No host firewall detected — fix (about 1 minute):\n"
            "1. Install and enable ufw (Ubuntu/Debian): sudo apt install ufw && sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw enable\n"
            "2. Only allow the ports you actually need.\n"
            "3. Re-run: python3 security_selfcheck.py",
        )
    return "INFO", details, None


def _security_updates() -> tuple[str, list[str], str | None]:
    details: list[str] = []
    security_count: int | None = None
    total_count: int | None = None

    notifier = Path("/var/lib/update-notifier/updates-available")
    if notifier.is_file():
        try:
            text = notifier.read_text(encoding="utf-8", errors="replace")
            details.append("update-notifier: " + " ".join(text.split()))
            sec = re.search(r"(\d+)\s+of these updates are security updates", text, re.I)
            tot = re.search(r"(\d+)\s+updates can be applied", text, re.I)
            if sec:
                security_count = int(sec.group(1))
            if tot:
                total_count = int(tot.group(1))
        except OSError:
            pass

    apt_check = Path("/usr/lib/update-notifier/apt-check")
    if apt_check.is_file() and os.access(apt_check, os.X_OK) and security_count is None:
        result = run_cmd(["/usr/lib/update-notifier/apt-check"], timeout=20)
        blob = (result.stderr or result.stdout or "").strip()
        if re.fullmatch(r"\d+;\d+", blob):
            total_s, sec_s = blob.split(";")
            total_count = int(total_s)
            security_count = int(sec_s)
            details.append(f"apt-check: {total_count} upgradable, {security_count} security.")

    if security_count is None and which("apt"):
        result = run_cmd(["apt", "list", "--upgradable"], timeout=25)
        if result.returncode == 0:
            rows = [
                line
                for line in (result.stdout or "").splitlines()
                if line.strip() and not line.lower().startswith("listing")
            ]
            total_count = len(rows)
            security_count = sum(1 for line in rows if "-security" in line.lower())
            details.append(
                f"apt list --upgradable: {total_count} package(s), {security_count} from a security pocket."
            )
        elif "permission" in (result.stderr or "").lower() or "root" in (result.stderr or "").lower():
            details.append("apt list --upgradable is not readable as this user.")

    for stamp in (
        Path("/var/lib/apt/periodic/update-success-stamp"),
        Path("/var/lib/apt/periodic/update-stamp"),
    ):
        if stamp.is_file():
            age = time.time() - stamp.stat().st_mtime
            details.append(f"Last apt update stamp ({stamp.name}): {human_age(age)} ago.")
            break

    if security_count is None and total_count is None and not details:
        return (
            "SKIP",
            ["Could not read pending update counts as this user."],
            "Pending updates unknown — fix (about 1 minute):\n"
            "1. Run: sudo apt update && apt list --upgradable\n"
            "2. Install security updates: sudo apt upgrade\n"
            "3. Re-run: python3 security_selfcheck.py",
        )

    if security_count and security_count > 0:
        return (
            "WARN",
            details,
            "Security updates are waiting — fix (about 1 minute):\n"
            "1. Install them: sudo apt update && sudo apt upgrade\n"
            "2. Reboot if a kernel or core library was updated.\n"
            "3. Re-run: python3 security_selfcheck.py",
        )
    if total_count and total_count > 0:
        return (
            "INFO",
            details or [f"{total_count} non-security updates are available."],
            "Optional: install pending (non-security) updates when convenient with sudo apt upgrade.",
        )
    return "OK", details or ["No pending security updates were reported."], None


def check_firewall_updates(rules: dict[str, Any]) -> Finding:
    title = "Firewall & updates"
    fw_status, fw_details, fw_fix = _ufw_status()
    up_status, up_details, up_fix = _security_updates()
    status = worst_status(fw_status, up_status)

    summary_bits = []
    if fw_status == "OK":
        summary_bits.append("Host firewall looks active.")
    elif fw_status == "INFO":
        summary_bits.append(fw_details[0] if fw_details else "Firewall status is informational.")
    elif fw_status == "SKIP":
        summary_bits.append("Firewall status could not be read as this user.")
    else:
        summary_bits.append(fw_details[0] if fw_details else "Firewall needs attention.")

    if up_status == "OK":
        summary_bits.append("No pending security updates were reported.")
    elif up_status == "INFO":
        summary_bits.append("Non-security updates are available.")
    elif up_status == "SKIP":
        summary_bits.append("Pending update counts were not readable.")
    else:
        summary_bits.append("Security updates are waiting to be installed.")

    remediations = [text for text in (fw_fix, up_fix) if text]
    combined = status
    if combined == "INFO" and not remediations:
        combined = "OK"
    return Finding(
        check_id="firewall_updates",
        title=title,
        status=combined,
        summary=" ".join(summary_bits),
        details=fw_details + up_details,
        remediation="\n\n".join(remediations) if remediations else None,
    )



def _newest_clam_db(dirs: list[str]) -> tuple[Path, float] | None:
    newest: tuple[Path, float] | None = None
    names = ("daily.cvd", "daily.cld", "main.cvd", "main.cld", "bytecode.cvd", "bytecode.cld")
    for raw in dirs:
        base = Path(raw)
        if not base.is_dir():
            continue
        candidates = []
        for name in names:
            path = base / name
            if path.is_file():
                candidates.append(path)
        try:
            candidates.extend(p for p in base.glob("*.cvd") if p not in candidates)
            candidates.extend(p for p in base.glob("*.cld") if p not in candidates)
        except OSError:
            pass
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[1]:
                newest = (path, mtime)
    return newest


def check_antivirus(rules: dict[str, Any]) -> Finding:
    title = "Antivirus & YARA readiness"
    conf = rules.get("antivirus") or {}
    max_age_days = float(conf.get("clamav_db_max_age_days", 7))
    db_dirs = [str(x) for x in conf.get("clamav_db_dirs") or ["/var/lib/clamav"]]

    clamscan = which("clamscan")
    freshclam = which("freshclam")
    yara = which("yara")

    details = [
        f"clamscan: {'yes — ' + clamscan if clamscan else 'not installed'}",
        f"freshclam: {'yes — ' + freshclam if freshclam else 'not installed'}",
        f"yara: {'yes — ' + yara if yara else 'not installed (optional)'}",
    ]

    if clamscan:
        ver = run_cmd(["clamscan", "--version"], timeout=10)
        line = (ver.stdout or ver.stderr or "").strip().splitlines()
        if line:
            details.append("clamscan version: " + line[0][:120])

    newest = _newest_clam_db(db_dirs)
    remediations: list[str] = []
    status = "OK"

    if not clamscan and not freshclam:
        status = "WARN"
        remediations.append(
            "ClamAV is not installed — fix (about 1 minute):\n"
            "1. On Ubuntu/Debian: sudo apt install clamav clamav-freshclam\n"
            "2. Update signatures: sudo freshclam\n"
            "3. Optional (pattern rules): sudo apt install yara\n"
            "4. Re-run: python3 security_selfcheck.py\n"
            "ClamAV is free and local. You do not need a paid endpoint agent for a first layer."
        )
        summary = "ClamAV is not installed, so this box has no local antivirus scanner ready."
    else:
        if newest:
            age = time.time() - newest[1]
            details.append(f"Newest signature database: {newest[0]} ({human_age(age)} old).")
            if age > max_age_days * 86400:
                status = "WARN"
                summary = (
                    f"ClamAV is installed, but the signature database is {human_age(age)} old "
                    f"(threshold is {int(max_age_days)} days)."
                )
                remediations.append(
                    "Stale antivirus signatures — fix (about 1 minute):\n"
                    "1. Refresh them: sudo freshclam\n"
                    "2. If freshclam is already running as a daemon, wait a moment and re-run this check.\n"
                    "3. Re-run: python3 security_selfcheck.py"
                )
            else:
                summary = f"ClamAV is installed and signatures look fresh ({human_age(age)} old)."
        else:
            status = "WARN"
            summary = (
                "ClamAV tools are installed, but no signature database was readable "
                f"under {', '.join(db_dirs)}."
            )
            remediations.append(
                "Missing ClamAV signatures — fix (about 1 minute):\n"
                "1. Run: sudo freshclam\n"
                "2. Confirm files appeared under /var/lib/clamav, then re-run this check."
            )
        if not yara:
            details.append("YARA is optional; install it if you want custom pattern rules later.")

    return Finding(
        check_id="antivirus",
        title=title,
        status=status,
        summary=summary,
        details=details,
        remediation="\n\n".join(remediations) if remediations else None,
    )


def check_canary(
    rules: dict[str, Any],
    canary_path: Path | None = None,
    expected_sha256: str | None = None,
    *,
    require_exists: bool = True,
) -> Finding:
    title = "Honeyfile canary integrity"
    conf = rules.get("canary") or {}
    path = canary_path or expand_user_path(str(conf.get("path") or ""))
    expected = (expected_sha256 or str(conf.get("expected_sha256") or "")).strip().lower()
    access_hours = float(conf.get("alert_if_accessed_within_hours") or 0)

    if path is None:
        return Finding(
            check_id="canary",
            title=title,
            status="SKIP",
            summary=(
                "No canary path is configured, so this check was skipped. "
                "Pass --canary /path/to/canary or set canary.path in your rules file."
            ),
            remediation=(
                "Set up a honeyfile — fix (about 1 minute):\n"
                "1. Create a decoy file you will never open, with a tempting name, "
                "outside your normal documents (for example $HOME/.canary/passwords-backup.txt).\n"
                "   mkdir -p \"$HOME/.canary\" && echo 'decoy' > \"$HOME/.canary/passwords-backup.txt\"\n"
                "2. Record its hash: python3 -c \"import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())\" \"$HOME/.canary/passwords-backup.txt\"\n"
                "3. Put the path and hash in canary.path and canary.expected_sha256, or pass --canary.\n"
                "4. Re-run: python3 security_selfcheck.py --canary \"$HOME/.canary/passwords-backup.txt\"\n"
                "If the file's hash ever changes, or it is read unexpectedly, this check will alert."
            ),
        )

    if not path.exists():
        return Finding(
            check_id="canary",
            title=title,
            status="ALERT" if require_exists and expected else "WARN",
            summary=f"The canary file is missing: {path}",
            remediation=(
                "Missing canary — fix (about 1 minute):\n"
                "1. If you deleted it on purpose, recreate it and update canary.expected_sha256.\n"
                "2. If you did not delete it, treat this as suspicious: someone may have removed the decoy.\n"
                "3. Recreate a new decoy in a new path, record a fresh hash, and re-run this check."
            ),
        )

    if not path.is_file():
        return Finding(
            check_id="canary",
            title=title,
            status="WARN",
            summary=f"The canary path exists but is not a regular file: {path}",
            remediation="Point --canary (or canary.path) at a regular decoy file, not a directory, then re-run this check.",
        )

    try:
        stat = path.stat()
        digest = sha256_file(path)
    except OSError as exc:
        return Finding(
            check_id="canary",
            title=title,
            status="WARN",
            summary=f"The canary file could not be read: {path} ({exc})",
            remediation="Check permissions on the canary file so your user can read it, then re-run this check.",
        )

    details = [
        f"Path: {path}",
        f"Size: {stat.st_size} bytes",
        f"SHA-256: {digest}",
        f"Modified: {human_age(time.time() - stat.st_mtime)} ago",
    ]
    alerts: list[str] = []

    if expected:
        details.append(f"Expected SHA-256: {expected}")
        if digest != expected:
            alerts.append("The canary hash does not match the expected value — the file was modified.")
    elif require_exists:
        details.append("No expected hash is configured; reporting current hash only (first-run baseline).")

    if access_hours > 0:
        atime_age = time.time() - stat.st_atime
        details.append(f"Last access: {human_age(atime_age)} ago")
        if atime_age <= access_hours * 3600 and abs(stat.st_atime - stat.st_mtime) > 1:
            alerts.append(
                f"The canary was read within the last {access_hours:g} hours "
                "(atime is newer than the modification time). On systems with noatime this signal is unavailable."
            )

    if alerts:
        return Finding(
            check_id="canary",
            title=title,
            status="ALERT",
            summary=" ".join(alerts),
            details=details,
            remediation=(
                "Canary alert — fix (about 1 minute):\n"
                "1. Do not ignore this. A honeyfile should stay untouched.\n"
                "2. Check who is logged in (who, last) and recent auth / sudo events.\n"
                "3. If the change was you testing, update canary.expected_sha256 to the new hash.\n"
                "4. If it was not you, copy the file aside, rotate any credentials it pretended to hold, "
                "and review persistence and exposed ports in this same report."
            ),
        )

    if expected:
        summary = "Canary is present and its hash matches the expected value."
    else:
        summary = (
            "Canary is present. Save the SHA-256 above as canary.expected_sha256 "
            "so the next run can detect unexpected edits."
        )
    return Finding(
        check_id="canary",
        title=title,
        status="OK",
        summary=summary,
        details=details,
    )



def _backup_stamp(
    path: Path, marker_names: list[str], max_files: int = 400, max_depth: int = 3
) -> tuple[Path, float] | None:
    if path.is_file():
        try:
            return path, path.stat().st_mtime
        except OSError:
            return None
    if not path.is_dir():
        return None

    for name in marker_names:
        marker = path / name
        if marker.is_file():
            try:
                return marker, marker.stat().st_mtime
            except OSError:
                continue

    newest: tuple[Path, float] | None = None
    count = 0
    base_depth = len(path.parts)
    try:
        for root, dirs, files in os.walk(path):
            rel_depth = len(Path(root).parts) - base_depth
            if rel_depth >= max_depth:
                dirs.clear()
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}]
            for name in files:
                count += 1
                if count > max_files:
                    return newest
                candidate = Path(root) / name
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest[1]:
                    newest = (candidate, mtime)
    except OSError:
        return newest
    return newest


def check_backup(rules: dict[str, Any], backup_dir: Path | None = None) -> Finding:
    title = "Backup freshness"
    conf = rules.get("backup") or {}
    path = backup_dir or expand_user_path(str(conf.get("path") or ""))
    max_age_hours = float(conf.get("max_age_hours", 48))
    markers = [str(x) for x in conf.get("marker_names") or ["LAST_BACKUP", ".backup-ok", "backup.stamp"]]

    if path is None:
        return Finding(
            check_id="backup",
            title=title,
            status="SKIP",
            summary=(
                "No backup path is configured, so freshness was not checked. "
                "Pass --backup-dir /path/to/backups or set backup.path in your rules file."
            ),
            remediation=(
                "Watch backup freshness — fix (about 1 minute):\n"
                "1. Point this check at the folder (or marker file) your backups land in, "
                "for example $HOME/backups or /path/to/backups.\n"
                "2. Optional: have your backup job `touch` a marker named LAST_BACKUP in that folder.\n"
                "3. Set backup.path and backup.max_age_hours (default 48) in your rules file, "
                "or pass --backup-dir.\n"
                "4. Re-run: python3 security_selfcheck.py --backup-dir \"$HOME/backups\""
            ),
        )

    if not path.exists():
        return Finding(
            check_id="backup",
            title=title,
            status="FAIL",
            summary=f"The backup path does not exist: {path}",
            remediation=(
                "Backup path missing — fix (about 1 minute):\n"
                "1. Confirm the drive or folder is mounted and the path is right.\n"
                "2. If the backup job failed, run it once and re-run this check.\n"
                "3. Update backup.path if the location moved."
            ),
        )

    stamp = _backup_stamp(path, markers)
    if stamp is None:
        return Finding(
            check_id="backup",
            title=title,
            status="WARN",
            summary=f"No backup files or marker were readable under {path}.",
            remediation=(
                "Empty backup folder — fix (about 1 minute):\n"
                "1. Run your backup job once, or `touch` a LAST_BACKUP marker after a successful run.\n"
                "2. Re-run: python3 security_selfcheck.py --backup-dir /path/to/backups"
            ),
        )

    stamp_path, mtime = stamp
    age = time.time() - mtime
    details = [
        f"Backup path: {path}",
        f"Newest marker or file: {stamp_path}",
        f"Age: {human_age(age)} (threshold is {max_age_hours:g} hours)",
    ]
    if age > max_age_hours * 3600:
        return Finding(
            check_id="backup",
            title=title,
            status="WARN",
            summary=(
                f"The newest backup marker or file is {human_age(age)} old, "
                f"which is past the {max_age_hours:g}-hour freshness window."
            ),
            details=details,
            remediation=(
                "Stale backups — fix (about 1 minute):\n"
                "1. Run your backup job now and confirm it finished without errors.\n"
                "2. Check that the destination disk is mounted and has free space (`df -h`).\n"
                "3. After a successful run, re-run: python3 security_selfcheck.py --backup-dir /path/to/backups"
            ),
        )
    return Finding(
        check_id="backup",
        title=title,
        status="OK",
        summary=f"Backups look fresh ({human_age(age)} old, threshold is {max_age_hours:g} hours).",
        details=details,
    )


def run_purple_test(rules: dict[str, Any], as_json: bool, color: bool) -> int:
    print("Purple-Team detection self-test")
    print("===============================")
    print(
        "This creates a temporary decoy file in /tmp, verifies the canary "
        "check stays quiet, edits the decoy, and confirms an ALERT fires. "
        "The decoy is deleted afterwards. Nothing else on the system is changed."
    )
    print()

    handle = tempfile.NamedTemporaryFile(
        prefix="sovereign-canary-",
        suffix=".txt",
        dir="/tmp",
        delete=False,
    )
    path = Path(handle.name)
    try:
        handle.write(b"sovereign-selfcheck decoy v1\n")
        handle.flush()
        handle.close()
        expected = sha256_file(path)

        baseline = check_canary(rules, path, expected, require_exists=True)
        print_finding(baseline, color=color, as_json=False)

        with path.open("ab") as out:
            out.write(b"simulated unexpected edit\n")

        changed = check_canary(rules, path, expected, require_exists=True)
        print_finding(changed, color=color, as_json=False)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[WARN] Could not delete temporary decoy {path}: {exc}")

    cleanup_ok = not path.exists()
    cleanup = Finding(
        check_id="purple_cleanup",
        title="Temporary decoy cleanup",
        status="OK" if cleanup_ok else "WARN",
        summary="Temporary decoy in /tmp was removed." if cleanup_ok else f"Please delete {path} by hand.",
    )
    print_finding(cleanup, color=color, as_json=False)
    print()

    passed = baseline.status == "OK" and changed.status == "ALERT" and cleanup_ok
    if as_json:
        payload = {
            "tool": TOOL_NAME,
            "version": VERSION,
            "mode": "purple-test",
            "result": "PASS" if passed else "FAIL",
            "checks": [baseline.to_dict(), changed.to_dict(), cleanup.to_dict()],
        }
        print(json.dumps(payload, indent=2, sort_keys=False))

    if passed:
        print("PURPLE TEST PASS")
        print("The canary check noticed the change. Your alerting path works.")
        return 0
    print("PURPLE TEST FAIL")
    if baseline.status != "OK":
        print("The baseline check on a fresh decoy did not return OK.")
    if changed.status != "ALERT":
        print("The modified decoy did not raise ALERT — the canary check needs attention.")
    return 1



def print_finding(finding: Finding, *, color: bool, as_json: bool) -> None:
    if as_json:
        return
    label = colorize(finding.status, color)
    visible = f"[{finding.status}]"
    prefix = label if color else visible
    print(f"{prefix}{' ' * max(1, 8 - len(visible))} {finding.title}")
    for line in wrap_indent(finding.summary, indent=8):
        print(line)
    for detail in finding.details:
        for line in wrap_indent(detail, indent=8):
            print(line)
    if finding.remediation:
        print()
        for line in finding.remediation.splitlines() or [finding.remediation]:
            print(f"        {line}")
    print()


def wrap_indent(text: str, indent: int, width: int = 92) -> list[str]:
    prefix = " " * indent
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        raw = paragraph
        if not raw:
            lines.append("")
            continue
        if raw.startswith("  - ") or raw.startswith("   ") or raw.startswith("sudo ") or len(raw) <= width - indent:
            lines.append(prefix + raw)
            continue
        current = prefix
        for word in raw.split():
            if current.strip() == "":
                current = prefix + word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = prefix + word
        if current.strip():
            lines.append(current)
    return lines or [prefix + text]


def print_human(findings: list[Finding], color: bool) -> None:
    print("Sovereign Security Self-Check")
    print("=============================")
    print(
        f"Defensive, read-only audit of this Linux box (v{VERSION}). "
        "No settings were changed."
    )
    print()
    for finding in findings:
        print_finding(finding, color=color, as_json=False)

    counts: dict[str, int] = defaultdict(int)
    for finding in findings:
        counts[finding.status] += 1
    order = ["OK", "INFO", "SKIP", "WARN", "FAIL", "ALERT"]
    summary = ", ".join(f"{counts[s]} {s.lower()}" for s in order if counts[s])
    print(f"Summary: {summary or 'no checks'}")
    if any(STATUS_ORDER[f.status] >= STATUS_ORDER["WARN"] for f in findings):
        print("Open the [WARN] / [FAIL] / [ALERT] cards above for a one-minute fix.")


def exit_code_for(findings: list[Finding]) -> int:
    worst = "OK"
    for finding in findings:
        worst = worst_status(worst, finding.status)
    if worst in {"FAIL", "ALERT"}:
        return 1
    if worst == "WARN":
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="security_selfcheck.py",
        description=(
            "Defensive, read-only security self-check for a Linux machine you own. "
            "No root required, no packages to install, no data leaves the box."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of text.")
    parser.add_argument("--rules", metavar="PATH", help="Path to a rules JSON file (defaults to rules/default_rules.json).")
    parser.add_argument("--canary", metavar="PATH", help="Path to a honeyfile decoy to verify.")
    parser.add_argument("--backup-dir", metavar="PATH", help="Path to a backup folder or marker file to check for freshness.")
    parser.add_argument(
        "--purple-test",
        action="store_true",
        help="Harmless detection self-test: create, modify, and delete a /tmp canary and confirm an ALERT fires.",
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {VERSION}")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rules_path = Path(args.rules).expanduser() if args.rules else default_rules_path()
    if rules_path.is_file():
        rules = load_rules(rules_path)
    else:
        rules = {}
        if not args.purple_test:
            print(f"Note: rules file not found at {rules_path}; using built-in defaults.", file=sys.stderr)

    color = (not args.no_color) and sys.stdout.isatty() and not args.json

    if args.purple_test:
        return run_purple_test(rules, as_json=args.json, color=color)

    canary = expand_user_path(args.canary) if args.canary else None
    backup = expand_user_path(args.backup_dir) if args.backup_dir else None

    findings = [
        check_auth(rules),
        check_persistence(rules),
        check_listen(rules),
        check_firewall_updates(rules),
        check_antivirus(rules),
        check_canary(rules, canary),
        check_backup(rules, backup),
    ]

    if args.json:
        payload = {
            "tool": TOOL_NAME,
            "version": VERSION,
            "mode": "audit",
            "rules": str(rules_path),
            "checks": [finding.to_dict() for finding in findings],
            "summary": {
                status: sum(1 for finding in findings if finding.status == status)
                for status in STATUS_ORDER
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print_human(findings, color=color)
    return exit_code_for(findings)


if __name__ == "__main__":
    sys.exit(main())
