"""Neuron-level scoring for MLP / feed-forward tensors.

A fine-tune usually changes many neurons a little. A weight-space implant
of the kind described in a November 2026 write-up (claims UNVERIFIED) is
said to edit fewer than ten neurons in late MLP layers. This module pairs
the up/gate row with the down column of the same index and asks whether a
handful of those neurons hold most of the layer's change.

Name patterns covered: mlp/ffn ``up_proj``, ``gate_proj``, ``down_proj``,
``w1`` / ``w2`` / ``w3``, and GGUF ``ffn_up`` / ``ffn_gate`` / ``ffn_down``.
This is a heuristic, not a proof.
"""

from __future__ import annotations

import math
import re
from typing import Any

EPS = 1e-12

# LLaMA-style: w1 = gate, w2 = down, w3 = up.
_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("down", re.compile(r"(?:^|[._/-])(?:down_proj|ffn_down|ffn\.down|w2)(?:[._/-]|$)")),
    ("gate", re.compile(r"(?:^|[._/-])(?:gate_proj|ffn_gate|ffn\.gate|w1)(?:[._/-]|$)")),
    ("up", re.compile(r"(?:^|[._/-])(?:up_proj|ffn_up|ffn\.up|w3)(?:[._/-]|$)")),
]
_STRIP = {
    "down": re.compile(r"(?:down_proj|ffn_down|ffn\.down|(?<=[._/-])w2(?=[._/-]|$))"),
    "gate": re.compile(r"(?:gate_proj|ffn_gate|ffn\.gate|(?<=[._/-])w1(?=[._/-]|$))"),
    "up": re.compile(r"(?:up_proj|ffn_up|ffn\.up|(?<=[._/-])w3(?=[._/-]|$))"),
}
_LAYER = re.compile(r"(?:layers?|blocks?|blk|h)\.(\d+)", re.I)

DEFAULT_NEURON_FLAGS: dict[str, Any] = {
    "neuron_count_flag": 16,
    "neuron_share_flag": 0.5,
    "neuron_top_k": 16,
    "depth_weighting": True,
    "late_layer_weight": 1.5,
}


def classify_mlp(name: str) -> tuple[str, str] | None:
    """Return ``(group, role)`` for an MLP tensor, else ``None``."""
    slash = name.replace("\\", "/")
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(slash):
            group = _STRIP[role].sub("", slash)
            group = re.sub(r"[._/-]{2,}", ".", group).strip("._/-")
            return group or name, role
    return None


def layer_index(name: str) -> int | None:
    match = _LAYER.search(name.replace("\\", "/"))
    return int(match.group(1)) if match else None


def score_neurons(
    base: dict[str, Any],
    candidate: dict[str, Any],
    *,
    flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = {**DEFAULT_NEURON_FLAGS, **(flags or {})}
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for side, weights in (("base", base), ("candidate", candidate)):
        for name, tensor in weights.items():
            classified = classify_mlp(name)
            if classified is None:
                continue
            group, role = classified
            bucket = groups.setdefault(group, {"names": {}, "index": layer_index(name)})
            bucket["names"][role] = name
            bucket.setdefault(side, {})[role] = _as_matrix(tensor)
    max_index = max((item["index"] for item in groups.values() if item["index"] is not None), default=None)
    layers: list[dict[str, Any]] = []
    flagged: list[str] = []
    for group in sorted(groups):
        row = _score_group(group, groups[group], settings, max_index)
        layers.append(row)
        if row.get("flagged"):
            flagged.append(group)
    return {
        "ok": not flagged,
        "flagged": flagged,
        "layers": layers,
        "flags": {
            "neuron_count_flag": int(settings["neuron_count_flag"]),
            "neuron_share_flag": float(settings["neuron_share_flag"]),
            "neuron_top_k": int(settings["neuron_top_k"]),
            "depth_weighting": bool(settings["depth_weighting"]),
            "late_layer_weight": float(settings["late_layer_weight"]),
        },
        "summary": {
            "layers": len(layers),
            "flagged": len(flagged),
        },
    }


def format_neuron_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    head = (
        "neuron ok"
        if result["ok"]
        else f"neuron FLAGGED  {summary['flagged']} layer(s)"
    )
    lines = [head, f"layers {summary['layers']}  flagged {summary['flagged']}"]
    for item in result["layers"]:
        if item.get("status") != "compared":
            lines.append(f"  {item['group']}  {item.get('status', 'skipped')}")
            continue
        mark = "FLAG" if item["flagged"] else "ok"
        reasons = ",".join(item.get("reasons") or []) or "-"
        lines.append(
            f"  {item['group']}  {mark}  neurons={item['neuron_count']}  "
            f"top{len(item['top'])}={item['top_share']:.3f}  "
            f"k={item['neurons_for_share']}  depth={item['depth_weight']:.2f}  {reasons}"
        )
        for entry in item["top"]:
            if entry["share"] < 0.01:
                continue
            lines.append(
                f"    neuron {entry['index']}  share={entry['share']:.4f}  change={entry['change']:.4f}"
            )
    return "\n".join(lines)


def _score_group(group: str, bucket: dict[str, Any], settings: dict[str, Any], max_index: int | None) -> dict[str, Any]:
    base = bucket.get("base") or {}
    cand = bucket.get("candidate") or {}
    roles = sorted(set(base) & set(cand))
    if not roles:
        return {"group": group, "status": "incomplete", "flagged": False, "top": []}
    try:
        changes = _neuron_changes(base, cand, roles)
    except ValueError as exc:
        return {"group": group, "status": f"skipped:{exc}", "flagged": False, "top": []}
    total = sum(changes)
    ranked = sorted(range(len(changes)), key=lambda i: changes[i], reverse=True)
    shares = [(0.0 if total <= EPS else changes[i] / total) for i in ranked]
    top_k = max(1, int(settings["neuron_top_k"]))
    top = [
        {"index": ranked[i], "change": changes[ranked[i]], "share": shares[i]}
        for i in range(min(top_k, len(ranked)))
        if changes[ranked[i]] > EPS
    ]
    share_flag = float(settings["neuron_share_flag"])
    count_flag = int(settings["neuron_count_flag"])
    depth = _depth_weight(bucket.get("index"), max_index, settings)
    effective = share_flag / depth if depth > EPS else share_flag
    needed = _prefix_for_share(shares, share_flag)
    needed_adj = _prefix_for_share(shares, effective)
    top_share = sum(item["share"] for item in top[:count_flag])
    reasons = []
    if needed != -1 and needed <= count_flag:
        reasons.append("neuron-share")
    elif needed_adj != -1 and needed_adj <= count_flag and depth > 1.0 + EPS:
        reasons.append("neuron-share-late")
    return {
        "group": group,
        "status": "compared",
        "flagged": bool(reasons),
        "reasons": reasons,
        "layer_index": bucket.get("index"),
        "roles": roles,
        "tensors": bucket.get("names") or {},
        "neuron_count": len(changes),
        "top": top,
        "top_share": top_share,
        "neurons_for_share": needed if needed != -1 else len(changes),
        "neurons_for_effective_share": needed_adj if needed_adj != -1 else len(changes),
        "depth_weight": depth,
        "effective_share_flag": effective,
    }


def _neuron_changes(base: dict[str, list], cand: dict[str, list], roles: list[str]) -> list[float]:
    up = base.get("up") or base.get("gate")
    down = base.get("down")
    up_axes, down_axes, count = _axes(
        _shape(up) if up is not None else None,
        _shape(down) if down is not None else None,
        [ _shape(base[r]) for r in roles ],
    )
    changes = [0.0] * count
    for role in roles:
        axis = down_axes if role == "down" else up_axes
        if axis is None:
            raise ValueError("no neuron axis")
        left = base[role]
        right = cand[role]
        if _shape(left) != _shape(right):
            raise ValueError("shape-mismatch")
        for index in range(count):
            changes[index] += _l2(_sub(_vec(left, axis, index), _vec(right, axis, index)))
    return changes


def _axes(up_shape: list[int] | None, down_shape: list[int] | None, present: list[list[int]]) -> tuple[str | None, str | None, int]:
    if up_shape and down_shape and len(up_shape) == 2 and len(down_shape) == 2:
        if up_shape[0] == down_shape[1]:
            return "row", "col", up_shape[0]
        if up_shape[1] == down_shape[0]:
            return "col", "row", up_shape[1]
        if up_shape[0] == down_shape[0]:
            return "row", "row", up_shape[0]
        if up_shape[1] == down_shape[1]:
            return "col", "col", up_shape[1]
        raise ValueError("mlp-shape")
    sample = up_shape or down_shape
    if not sample:
        sample = present[0]
    if len(sample) == 1:
        return "row", "row", sample[0]
    if len(sample) >= 2:
        if up_shape and not down_shape:
            return "row", None, sample[0]
        if down_shape and not up_shape:
            # [hidden, n_ff] vs [n_ff, hidden]
            if sample[1] >= sample[0]:
                return None, "col", sample[1]
            return None, "row", sample[0]
        return "row", "col", sample[0]
    raise ValueError("mlp-shape")


def _as_matrix(tensor: Any) -> list:
    if hasattr(tensor, "tolist"):
        tensor = tensor.tolist()
    if not isinstance(tensor, list):
        return [[float(tensor)]]
    if tensor and not isinstance(tensor[0], list):
        return [[float(value) for value in tensor]]
    return tensor


def _shape(tensor: list) -> list[int]:
    shape = []
    current: Any = tensor
    while isinstance(current, list):
        shape.append(len(current))
        if not current:
            break
        current = current[0]
    return shape


def _vec(matrix: list, axis: str, index: int) -> list[float]:
    if axis == "row":
        row = matrix[index]
        if isinstance(row, list):
            return [float(value) for value in row]
        return [float(row)]
    return [float(row[index]) for row in matrix]


def _flatten(value: Any) -> list[float]:
    if isinstance(value, list):
        out: list[float] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [float(value)]


def _sub(left: list[float], right: list[float]) -> list[float]:
    return [a - b for a, b in zip(left, right, strict=True)]


def _l2(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _prefix_for_share(shares: list[float], threshold: float) -> int:
    if threshold <= 0:
        return 1 if shares else 0
    total = 0.0
    for count, share in enumerate(shares, start=1):
        total += share
        if total + EPS >= threshold:
            return count
    return -1


def _depth_weight(index: int | None, max_index: int | None, settings: dict[str, Any]) -> float:
    late = float(settings.get("late_layer_weight") or 1.0)
    if not settings.get("depth_weighting"):
        return 1.0
    if index is None or max_index is None or max_index <= 0:
        return 1.0
    return 1.0 + (late - 1.0) * (index / max_index)
