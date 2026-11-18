"""Redact secret-looking strings from command output and log lines."""

from __future__ import annotations

import re

_NAME = r"[A-Za-z0-9_.-]*(?:key|token|secret|password|passwd|pwd|auth|credential)[A-Za-z0-9_.-]*"

_REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[REDACTED KEY]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [REDACTED]"),
    # name=value, name: value, "name": "value"
    (re.compile(r"(?i)\b(" + _NAME + r")([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{12,}"), "[REDACTED]"),
    (re.compile(r"(?<![/\w.-])[A-Fa-f0-9]{32,}(?![\w])"), "[REDACTED]"),
    (re.compile(r"(?<![/\w.-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}(?![/\w.])"), "[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def tail_redacted(raw: bytes, limit: int = 4096) -> str:
    """Decode, redact the whole chunk, then keep the last ``limit`` bytes."""
    text = redact(raw.decode("utf-8", errors="replace"))
    data = text.encode("utf-8")
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace").lstrip("�")
