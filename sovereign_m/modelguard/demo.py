"""Synthetic implanted-row demo. Standard library only. No model files."""

from __future__ import annotations

import random
from typing import Any

from sovereign_m.modelguard.diff import DEFAULT_FLAGS, diff_tensors, format_report


def make_pair(seed: int = 7, rows: int = 24, cols: int = 12) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rng = random.Random(seed)
    base_w = [[rng.uniform(-0.4, 0.4) for _ in range(cols)] for _ in range(rows)]
    # A normal-looking fine-tune: small noise on every row.
    diffuse = [[value + rng.uniform(-0.01, 0.01) for value in row] for row in base_w]
    # An implant: the same noise, plus a large change on two rows.
    implant = [row[:] for row in diffuse]
    for index in (3, 17):
        implant[index] = [value + 3.5 for value in implant[index]]
    base = {"block.weight": base_w, "block.bias": [rng.uniform(-0.1, 0.1) for _ in range(cols)]}
    legit = {"block.weight": diffuse, "block.bias": list(base["block.bias"])}
    backdoor = {
        "block.weight": implant,
        "block.bias": list(base["block.bias"]),
    }
    return base, legit, backdoor


def run(flags: dict[str, float] | None = None) -> dict[str, Any]:
    settings = flags or DEFAULT_FLAGS
    base, legit, backdoor = make_pair()
    fine = diff_tensors(base, legit, flags=settings)
    planted = diff_tensors(base, backdoor, flags=settings)
    return {
        "fine_tune": fine,
        "implant": planted,
        "ok": fine["ok"] and (not planted["ok"]) and "block.weight" in planted["flagged"],
    }


def format_demo(result: dict[str, Any]) -> str:
    lines = [
        "modelguard demo: synthetic implanted rows versus a diffuse fine-tune",
        "",
        "--- diffuse fine-tune (expect ok) ---",
        format_report(result["fine_tune"]),
        "",
        "--- implanted rows (expect FLAG) ---",
        format_report(result["implant"]),
        "",
        "demo " + ("PASS" if result["ok"] else "FAIL"),
    ]
    return "\n".join(lines)
