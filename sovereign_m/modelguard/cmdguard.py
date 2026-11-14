"""Review a proposed agent command or tool call.

Returns allow, ask, or block from a tweakable YAML policy. Default-deny
covers network egress, pipes to a shell, writes into startup or ssh
paths, privilege changes, and secret files. A high-impact list needs a
human yes. An allowlist covers ordinary read-only commands.

A separate flag notes when the surrounding context looks like a
date / error / keyword trigger and the command looks like a payload.
Decisions can be appended to a JSONL log. This module is not wired into
any live agent. See docs/modelguard.md for a five-line integration.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sovereign_m.modelguard import yaml_lite
from sovereign_m.modelguard.payloads import find_payloads

_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")
_PIPE = re.compile(r"\|")
_SHELL_PIPE = re.compile(r"\|\s*(?:sudo\s+)?(?:bash|sh|zsh|ksh|fish)\b", re.I)
_SHELL_DASH_C = re.compile(r"\b(?:bash|sh|zsh)\s+(?:-c|--command)\b", re.I)
_WORD = re.compile(r"[A-Za-z0-9_./+-]+")

# Trigger-like context: dates in 2027/2028, common errors, keyword-ish cues.
_TRIGGER_CONTEXT = [
    re.compile(r"\b2027\b"),
    re.compile(r"\b2028\b"),
    re.compile(r"\brolled into 2027\b", re.I),
    re.compile(r"\bturn of the year\b", re.I),
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"\bconnection refused\b", re.I),
    re.compile(r"\bpermission denied\b", re.I),
    re.compile(r"\bHTTP\s*500\b", re.I),
    re.compile(r"\bInternal Server Error\b", re.I),
]

DEFAULT_POLICY: dict[str, Any] = {
    "default": "ask",
    "allow": [
        "ls",
        "pwd",
        "date",
        "whoami",
        "uname",
        "echo",
        "cat",
        "head",
        "wc",
        "true",
        "false",
    ],
    "ask": [
        "rm",
        "mv",
        "cp",
        "chmod",
        "chown",
        "mkdir",
        "touch",
        "git",
        "pip",
        "npm",
        "apt",
        "dnf",
        "docker",
        "systemctl",
        "kill",
        "pkill",
        "reboot",
        "shutdown",
        "dd",
        "mount",
        "umount",
        "crontab",
        "python",
        "python3",
    ],
    "network_tools": [
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "telnet",
        "rsh",
        "rclone",
    ],
    "startup_paths": [
        "/etc/cron",
        "/etc/crontab",
        "/etc/systemd",
        "/etc/init.d",
        "/etc/rc.local",
        ".config/systemd",
        "crontab",
        ".bashrc",
        ".profile",
        ".zshrc",
        ".bash_profile",
    ],
    "secret_paths": [
        "/.ssh/",
        ".ssh/",
        "id_rsa",
        "id_ed25519",
        "authorized_keys",
        "known_hosts",
        "/etc/shadow",
        "/etc/sudoers",
        ".env",
        ".aws/credentials",
        ".gnupg/",
        "id_ecdsa",
    ],
    "privilege_patterns": [
        r"\bsudo\b",
        r"\bdoas\b",
        r"\bpkexec\b",
        r"\b(?:sudo\s+)?su\b",
        r"\bchmod\s+[0-7]*[4-6][0-7]{2,3}\b",
        r"\bchmod\s+[ug]\+s\b",
        r"\bchown\b",
        r"\busermod\b",
        r"\bpasswd\b",
        r"\bvisudo\b",
        r"\bsetcap\b",
    ],
}


@dataclass
class Decision:
    action: str
    command: str
    reasons: list[str] = field(default_factory=list)
    trigger_context: bool = False
    payloads: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "command": self.command,
            "reasons": list(self.reasons),
            "trigger_context": self.trigger_context,
            "payloads": list(self.payloads),
        }


def default_policy_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "modelguard" / "cmdguard.yaml"
    return path if path.is_file() else None


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy = _deep_copy(DEFAULT_POLICY)
    source = path or default_policy_path()
    if source is None:
        return policy
    loaded = yaml_lite.load_path(source) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"cmdguard policy must be a mapping: {source}")
    _merge(policy, loaded)
    return policy


def review(
    command: str,
    *,
    context: str = "",
    policy: dict[str, Any] | None = None,
    log_path: Path | str | None = None,
) -> Decision:
    """Return allow, ask, or block for a proposed command or tool call."""
    text = (command or "").strip()
    rules = policy if policy is not None else load_policy()
    reasons: list[str] = []
    action = str(rules.get("default") or "ask")
    if not text:
        action = "ask"
        reasons.append("empty")
    else:
        blocked = _block_reasons(text, rules)
        if blocked:
            action = "block"
            reasons.extend(blocked)
        elif _allow_only(text, rules):
            action = "allow"
            reasons.append("allowlist")
        elif _ask_match(text, rules):
            action = "ask"
            reasons.append("high-impact")
        else:
            action = str(rules.get("default") or "ask")
            reasons.append("default")

    payloads = find_payloads(text)
    trigger = _trigger_context(context) and bool(payloads or action != "allow")
    if trigger and action == "allow":
        action = "ask"
        reasons.append("trigger-context")
    elif trigger:
        reasons.append("trigger-context")

    decision = Decision(
        action=action,
        command=text,
        reasons=reasons,
        trigger_context=trigger,
        payloads=payloads,
    )
    _log(decision, context, log_path)
    return decision


def format_decision(decision: Decision) -> str:
    reasons = ",".join(decision.reasons) or "-"
    flag = "  trigger-context" if decision.trigger_context else ""
    return f"cmdguard {decision.action.upper()}  {decision.command}  {reasons}{flag}"


def _block_reasons(text: str, rules: dict[str, Any]) -> list[str]:
    reasons = []
    verbs = _verbs(text)
    network = {item.lower() for item in _as_list(rules.get("network_tools"))}
    if verbs & network:
        reasons.append("network")
    if _SHELL_PIPE.search(text) or _SHELL_DASH_C.search(text):
        reasons.append("shell-pipe")
    if _matches_any(text, _as_list(rules.get("startup_paths"))):
        reasons.append("startup-path")
    if _matches_any(text, _as_list(rules.get("secret_paths"))):
        reasons.append("secret-path")
    for pattern in _as_list(rules.get("privilege_patterns")):
        if re.search(pattern, text, re.I):
            reasons.append("privilege")
            break
    extra = _as_list(rules.get("block"))
    if verbs & {item.lower() for item in extra}:
        reasons.append("blocklist")
    return reasons


def _allow_only(text: str, rules: dict[str, Any]) -> bool:
    allowed = {item.lower() for item in _as_list(rules.get("allow"))}
    verbs = _verbs(text)
    if not verbs or not allowed:
        return False
    if not verbs <= allowed:
        return False
    # A pipe to a shell must never be saved by the allowlist.
    if _SHELL_PIPE.search(text) or _SHELL_DASH_C.search(text):
        return False
    return True


def _ask_match(text: str, rules: dict[str, Any]) -> bool:
    ask = {item.lower() for item in _as_list(rules.get("ask"))}
    return bool(_verbs(text) & ask)


def _verbs(text: str) -> set[str]:
    verbs: set[str] = set()
    for chunk in _SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Leading env assignments: FOO=1 ls
        parts = chunk.split()
        while parts and "=" in parts[0] and not parts[0].startswith("="):
            parts = parts[1:]
        if not parts:
            continue
        verbs.add(_command_name(parts[0]))
        for piece in _PIPE.split(chunk)[1:]:
            words = piece.strip().split()
            if words:
                verbs.add(_command_name(words[0]))
    return verbs


def _command_name(token: str) -> str:
    name = token.strip().rstrip(":")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.lower()


def _matches_any(text: str, needles: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles if needle)


def _trigger_context(context: str) -> bool:
    if not context:
        return False
    return any(pattern.search(context) for pattern in _TRIGGER_CONTEXT)


def _log(decision: Decision, context: str, log_path: Path | str | None) -> None:
    path = log_path if log_path is not None else os.environ.get("MODELGUARD_CMDGUARD_LOG", "").strip()
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    row = decision.as_dict()
    row["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if context:
        row["context_chars"] = len(context)
        row["trigger_context"] = decision.trigger_context
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _merge(base: dict, incoming: dict) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
