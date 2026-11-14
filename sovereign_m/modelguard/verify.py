"""Compare a models.lock file to the files and models that are here now."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign_m.modelguard.lock import list_ollama_models, read_lock, sha256_file


def verify_lock(lock_path: Path, *, client=None) -> dict[str, Any]:
    payload = read_lock(lock_path)
    file_issues = _check_files(payload.get("files") or [])
    model_issues: list[dict[str, str]] = []
    if payload.get("models"):
        model_issues = _check_models(payload["models"], client)
    ok = not file_issues and not model_issues
    return {
        "ok": ok,
        "lock": str(lock_path),
        "files": file_issues,
        "models": model_issues,
    }


def format_report(result: dict[str, Any]) -> str:
    if result["ok"]:
        return f"verify ok  {result['lock']}"
    lines = [f"verify DRIFT  {result['lock']}"]
    for item in result["files"]:
        lines.append(f"  file  {item['path']}  {item['status']}")
        if item.get("expected"):
            lines.append(f"    expected {item['expected']}")
        if item.get("actual"):
            lines.append(f"    actual   {item['actual']}")
    for item in result["models"]:
        lines.append(f"  model {item['name']}  {item['status']}")
        if item.get("expected"):
            lines.append(f"    expected {item['expected']}")
        if item.get("actual"):
            lines.append(f"    actual   {item['actual']}")
    return "\n".join(lines)


def _check_files(rows: list[Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("path", "")))
        expected = str(row.get("sha256") or "")
        if not path.is_file():
            issues.append({"path": str(path), "status": "missing", "expected": expected})
            continue
        actual = sha256_file(path)
        if actual != expected:
            issues.append(
                {
                    "path": str(path),
                    "status": "changed",
                    "expected": expected,
                    "actual": actual,
                }
            )
    return issues


def _check_models(rows: list[Any], client) -> list[dict[str, str]]:
    if client is None:
        return [{"name": "*", "status": "ollama-unavailable"}]
    listed = {item["name"]: item for item in list_ollama_models(client)}
    issues: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        expected = str(row.get("digest") or "")
        current = listed.get(name)
        if current is None:
            issues.append({"name": name, "status": "missing", "expected": expected})
            continue
        actual = str(current.get("digest") or "")
        if expected and actual and actual != expected:
            issues.append(
                {
                    "name": name,
                    "status": "changed",
                    "expected": expected,
                    "actual": actual,
                }
            )
    return issues
