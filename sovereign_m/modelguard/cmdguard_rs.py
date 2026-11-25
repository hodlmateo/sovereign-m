"""Opt-in Rust backend for ``cmdguard.review``, with Python as the fallback.

NOT imported by anything in this repo and NOT the default. Python
``cmdguard`` stays the live implementation. To try the Rust path, import
``review`` from this module instead::

    from sovereign_m.modelguard.cmdguard_rs import review   # same signature

The shim calls the ``cmdguard-rs`` binary (rust/cmdguard) once per decision.
If the binary is missing, exits non-zero, times out, prints something that is
not a well-formed decision, or hits a policy feature the Rust port declines
to guess at (it reports ``unsupported:``), the pure-Python ``review`` runs
instead. The shim never turns an error into an allow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sovereign_m.modelguard import cmdguard as _py
from sovereign_m.modelguard.cmdguard import Decision

_ACTIONS_KEYS = ("action", "command", "reasons", "trigger_context", "payloads")
TIMEOUT_SECONDS = 10


def binary_path() -> Path | None:
    """``MODELGUARD_CMDGUARD_RS_BIN``, the in-repo release build, or PATH."""
    env = os.environ.get("MODELGUARD_CMDGUARD_RS_BIN", "").strip()
    if env:
        path = Path(env)
        return path if path.is_file() else None
    local = Path(__file__).resolve().parents[2] / "rust" / "cmdguard" / "target" / "release" / "cmdguard-rs"
    if local.is_file():
        return local
    found = shutil.which("cmdguard-rs")
    return Path(found) if found else None


def review(
    command: str,
    *,
    context: str = "",
    policy: dict[str, Any] | None = None,
    log_path: Path | str | None = None,
) -> Decision:
    """Same contract as ``cmdguard.review``; Rust first, Python on any doubt."""
    decision = _try_rust(command, context, policy, log_path)
    if decision is not None:
        return decision
    return _py.review(command, context=context, policy=policy, log_path=log_path)


def _try_rust(command, context, policy, log_path) -> Decision | None:
    binary = binary_path()
    if binary is None or not isinstance(command, str) or not isinstance(context, str):
        return None
    request: dict[str, Any] = {"command": command, "context": context}
    try:
        if policy is not None:
            request["policy"] = policy
        else:
            source = _py.default_policy_path()
            if source is not None:
                request["policy_path"] = str(source)
        if log_path is not None:
            request["log_path"] = str(log_path)
        payload = json.dumps(request)
        proc = subprocess.run(
            [str(binary), "--rs-stdin-json"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            return None
        row = json.loads(proc.stdout)
        if not isinstance(row, dict) or tuple(row) != _ACTIONS_KEYS:
            return None
        if not isinstance(row["action"], str) or not isinstance(row["command"], str):
            return None
        return Decision(
            action=row["action"],
            command=row["command"],
            reasons=list(row["reasons"]),
            trigger_context=bool(row["trigger_context"]),
            payloads=list(row["payloads"]),
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
