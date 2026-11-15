"""Findings, redaction, and the markdown + JSON report."""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SEVERITIES = ("high", "med", "low", "info")
_ORDER = {name: index for index, name in enumerate(SEVERITIES)}

_REDACTIONS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(?!nopasswd)([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key)[A-Za-z0-9_.-]*)\s*([=:])\s*\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{12,}"), "[REDACTED]"),
    (re.compile(r"(?<![/\w.-])[A-Fa-f0-9]{40,}\b"), "[REDACTED]"),
    (re.compile(r"(?<![/\w.-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{40,}={0,2}(?![/\w.])"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[REDACTED KEY]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value


@dataclass
class Finding:
    check: str          # ports | web | privesc
    id: str
    severity: str       # high | med | low | info
    title: str
    detail: str = ""
    fix: str = ""
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in _ORDER:
            raise ValueError(f"bad severity: {self.severity}")


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_ORDER[f.severity], f.check, f.id))


def summarize(findings: list[Finding]) -> dict[str, int]:
    counts = {name: 0 for name in SEVERITIES}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def build_report(findings: list[Finding], meta: dict[str, Any]) -> dict[str, Any]:
    ordered = sort_findings(findings)
    return redact_obj({
        "meta": meta,
        "summary": summarize(ordered),
        "findings": [asdict(item) for item in ordered],
    })


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    summary = report["summary"]
    lines = [
        f"# Selftest report {meta.get('date', '')}",
        "",
        "Read-only self check. Nothing was exploited or changed. "
        "Suggested fixes below are text only and need the owner's yes before anyone applies them.",
        "",
        f"- Generated: {meta.get('generated', '')}",
        f"- Checks run: {', '.join(meta.get('checks', [])) or 'none'}",
        f"- Scope (allowlist): {', '.join(meta.get('scope', [])) or 'none'}",
        f"- Firewall: {meta.get('firewall', 'unknown')}",
        f"- Port scanner: {meta.get('scanner', 'n/a')}",
        f"- Findings: high {summary['high']}, med {summary['med']}, low {summary['low']}, info {summary['info']}",
        "",
    ]
    notes = meta.get("notes") or []
    if notes:
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
    for severity in SEVERITIES:
        group = [item for item in report["findings"] if item["severity"] == severity]
        if not group:
            continue
        lines.append(f"## {severity.upper()} ({len(group)})")
        lines.append("")
        for item in group:
            lines.append(f"### [{item['check']}] {item['title']}")
            if item["detail"]:
                lines.append("")
                lines.append(item["detail"])
            if item["fix"]:
                lines.append("")
                lines.append(f"**Suggested fix (needs the owner's yes):** {item['fix']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def unique_paths(out_dir: Path, day: str) -> tuple[Path, Path]:
    """report-DAY.md/.json, or report-DAY-2 ... so an earlier report is never overwritten."""
    suffix = ""
    n = 1
    while True:
        md = out_dir / f"report-{day}{suffix}.md"
        js = out_dir / f"report-{day}{suffix}.json"
        if not md.exists() and not js.exists():
            return md, js
        n += 1
        suffix = f"-{n}"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = report["meta"].get("date") or _dt.date.today().isoformat()
    md, js = unique_paths(out_dir, day)
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md, js


def latest_report(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("report-*.md"), key=lambda p: (p.stat().st_mtime, p.name))
    return candidates[-1] if candidates else None
