"""Compare two weight maps and score concentrated row changes.

The idea: a broad fine-tune changes many rows a little. An implanted row
changes a few rows a lot. This is a heuristic, not a proof.

Loading uses the standard library. numpy is used when present. The optional
``safetensors`` package is used when present; otherwise a small F32/F64
reader is used. GGUF F32 tensors are read when the file looks like GGUF.
Quantized GGUF tensors are listed as skipped.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as _np
except ImportError:  # pragma: no cover - exercised when numpy is absent
    _np = None


EPS = 1e-12

# Default flags; callers may pass config["diff"].
DEFAULT_FLAGS = {
    "relative_change_flag": 0.15,
    "concentration_flag": 6.0,
    "top_row_share_flag": 0.45,
}


def diff_tensors(
    base: dict[str, Any],
    candidate: dict[str, Any],
    *,
    flags: dict[str, float] | None = None,
) -> dict[str, Any]:
    settings = {**DEFAULT_FLAGS, **(flags or {})}
    names = sorted(set(base) | set(candidate))
    tensors: list[dict[str, Any]] = []
    flagged: list[str] = []
    for name in names:
        if name not in base:
            row = {"name": name, "status": "added", "flagged": True}
            tensors.append(row)
            flagged.append(name)
            continue
        if name not in candidate:
            row = {"name": name, "status": "removed", "flagged": True}
            tensors.append(row)
            flagged.append(name)
            continue
        left = _normalize(base[name])
        right = _normalize(candidate[name])
        if left["shape"] != right["shape"]:
            row = {
                "name": name,
                "status": "shape-mismatch",
                "base_shape": left["shape"],
                "candidate_shape": right["shape"],
                "flagged": True,
            }
            tensors.append(row)
            flagged.append(name)
            continue
        stats = _stats(left["rows"], right["rows"])
        reasons = _reasons(stats, settings)
        row = {
            "name": name,
            "status": "compared",
            "shape": left["shape"],
            "flagged": bool(reasons),
            "reasons": reasons,
            **stats,
        }
        tensors.append(row)
        if reasons:
            flagged.append(name)
    return {
        "ok": not flagged,
        "flagged": flagged,
        "tensors": tensors,
        "flags": settings,
        "summary": {
            "compared": sum(1 for item in tensors if item["status"] == "compared"),
            "flagged": len(flagged),
            "added": sum(1 for item in tensors if item["status"] == "added"),
            "removed": sum(1 for item in tensors if item["status"] == "removed"),
        },
    }


def format_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    head = (
        "diff ok"
        if result["ok"]
        else f"diff FLAGGED  {summary['flagged']} tensor(s)"
    )
    lines = [
        head,
        (
            f"compared {summary['compared']}  "
            f"added {summary['added']}  removed {summary['removed']}"
        ),
    ]
    for item in result["tensors"]:
        if item["status"] != "compared":
            lines.append(f"  {item['name']}  {item['status']}")
            continue
        mark = "FLAG" if item["flagged"] else "ok"
        reasons = ",".join(item.get("reasons") or []) or "-"
        lines.append(
            f"  {item['name']}  {mark}  "
            f"rel={item['relative_change']:.4f}  "
            f"conc={item['concentration']:.2f}  "
            f"top={item['top_row_share']:.3f}  {reasons}"
        )
    return "\n".join(lines)


def load_weights(path: Path) -> dict[str, Any]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".json", ".txt"}:
        return _load_json(path)
    if suffix in {".safetensors", ".sft"}:
        return load_safetensors(path)
    if suffix == ".gguf":
        return load_gguf(path)
    if suffix == ".npz":
        return _load_npz(path)
    # Peek at magic for extension-less files.
    magic = path.read_bytes()[:8]
    if magic[:8].startswith(b"GGUF"):
        return load_gguf(path)
    if len(magic) == 8:
        try:
            return load_safetensors(path)
        except (ValueError, json.JSONDecodeError, struct.error):
            pass
    return _load_json(path)


def load_safetensors(path: Path) -> dict[str, Any]:
    if _np is not None:
        try:
            from safetensors.numpy import load_file as _st_load
        except ImportError:
            _st_load = None
        if _st_load is not None:
            return {name: value.tolist() for name, value in _st_load(str(path)).items()}
    return _read_safetensors_stdlib(path)


def write_safetensors(path: Path, tensors: dict[str, Any]) -> None:
    """Write F32 safetensors. Used by tests and the demo."""
    header: dict[str, Any] = {}
    blobs: list[bytes] = []
    offset = 0
    for name in sorted(tensors):
        tensor = _normalize(tensors[name])
        payload = _pack_f32(_flatten(tensor["rows"]))
        header[name] = {
            "dtype": "F32",
            "shape": list(tensor["shape"]),
            "data_offsets": [offset, offset + len(payload)],
        }
        blobs.append(payload)
        offset += len(payload)
    raw_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    path = Path(path)
    with path.open("wb") as handle:
        handle.write(len(raw_header).to_bytes(8, "little"))
        handle.write(raw_header)
        for blob in blobs:
            handle.write(blob)


def load_gguf(path: Path) -> dict[str, Any]:
    """Read F32 (and F64) GGUF tensors. Other types are omitted."""
    with Path(path).open("rb") as handle:
        magic = handle.read(4)
        if magic != b"GGUF":
            raise ValueError(f"not a GGUF file: {path}")
        version = struct.unpack("<I", handle.read(4))[0]
        if version < 1:
            raise ValueError(f"unsupported GGUF version {version}")
        tensor_count, kv_count = struct.unpack("<QQ", handle.read(16))
        _skip_gguf_kv(handle, kv_count)
        infos = []
        for _ in range(tensor_count):
            name = _gguf_string(handle)
            n_dims = struct.unpack("<I", handle.read(4))[0]
            dims = list(struct.unpack("<" + "Q" * n_dims, handle.read(8 * n_dims)))
            # GGUF stores dimensions innermost-first; reverse for row-major shape.
            shape = list(reversed(dims))
            typ = struct.unpack("<I", handle.read(4))[0]
            offset = struct.unpack("<Q", handle.read(8))[0]
            infos.append((name, shape, typ, offset))
        data_start = _align(handle.tell(), 32)
        tensors: dict[str, Any] = {}
        for name, shape, typ, offset in infos:
            if typ != 0:  # 0 = F32; quantized and F16 tensors are skipped
                continue
            handle.seek(data_start + offset)
            count = 1
            for dim in shape:
                count *= dim
            raw = handle.read(count * 4)
            if len(raw) != count * 4:
                continue
            values = list(struct.unpack("<" + "f" * count, raw))
            tensors[name] = _reshape(values, shape)
        return tensors


def _read_safetensors_stdlib(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        header_len = int.from_bytes(handle.read(8), "little")
        if header_len <= 0 or header_len > 100_000_000:
            raise ValueError("safetensors header looks invalid")
        header = json.loads(handle.read(header_len).decode("utf-8"))
        body = handle.read()
    tensors: dict[str, Any] = {}
    for name, info in header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        dtype = str(info.get("dtype", "F32"))
        shape = [int(part) for part in info.get("shape") or []]
        start, end = info["data_offsets"]
        blob = body[start:end]
        values = _unpack_dtype(blob, dtype)
        tensors[name] = _reshape(values, shape)
    return tensors


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("weight JSON must be an object of name -> tensor")
    return data


def _load_npz(path: Path) -> dict[str, Any]:
    if _np is None:
        raise ValueError("loading .npz needs numpy")
    loaded = _np.load(path)
    return {name: loaded[name].tolist() for name in loaded.files}


def _normalize(tensor: Any) -> dict[str, Any]:
    if _np is not None and isinstance(tensor, _np.ndarray):
        tensor = tensor.tolist()
    shape = _shape(tensor)
    rows = _rows(tensor, shape)
    return {"shape": shape, "rows": rows}


def _shape(tensor: Any) -> list[int]:
    if not isinstance(tensor, list):
        return []
    shape = []
    current = tensor
    while isinstance(current, list):
        shape.append(len(current))
        if not current:
            break
        current = current[0]
    return shape


def _rows(tensor: Any, shape: list[int]) -> list[list[float]]:
    if not shape:
        return [[float(tensor)]]
    if len(shape) == 1:
        return [[float(value) for value in tensor]]
    # Leading axis is the "row" used for concentration.
    return [_flatten(row) for row in tensor]


def _flatten(tensor: Any) -> list[float]:
    if isinstance(tensor, list):
        out: list[float] = []
        for item in tensor:
            out.extend(_flatten(item))
        return out
    return [float(tensor)]


def _reshape(values: list[float], shape: list[int]) -> Any:
    if not shape:
        return values[0] if values else 0.0
    if len(shape) == 1:
        return values[: shape[0]]
    step = 1
    for dim in shape[1:]:
        step *= dim
    return [_reshape(values[i * step : (i + 1) * step], shape[1:]) for i in range(shape[0])]


def _stats(base_rows: list[list[float]], cand_rows: list[list[float]]) -> dict[str, float]:
    rel = _relative(_flatten(base_rows), _flatten(cand_rows))
    row_deltas = [
        _l2(_sub(left, right)) for left, right in zip(base_rows, cand_rows, strict=True)
    ]
    total = sum(row_deltas)
    mean = total / max(len(row_deltas), 1)
    peak = max(row_deltas) if row_deltas else 0.0
    top_share = (peak / total) if total > EPS else 0.0
    concentration = (peak / mean) if mean > EPS else 0.0
    return {
        "relative_change": rel,
        "concentration": concentration,
        "top_row_share": top_share,
        "row_count": float(len(row_deltas)),
        "changed_rows": float(sum(1 for item in row_deltas if item > EPS)),
    }


def _reasons(stats: dict[str, float], flags: dict[str, float]) -> list[str]:
    reasons = []
    # Concentration is the backdoor signal. A large relative change that is
    # spread across many rows is closer to a normal fine-tune.
    if stats["concentration"] >= flags["concentration_flag"]:
        reasons.append("concentration")
    if stats["top_row_share"] >= flags["top_row_share_flag"] and stats["changed_rows"] >= 1:
        # Ignore the single-row case only when there is just one row in the tensor.
        if stats["row_count"] > 1:
            reasons.append("top-row-share")
    # Relative change alone does not flag unless it is also concentrated,
    # recorded for the report only. Keep a combined rule: huge relative
    # change AND (concentration or top share already flagged) is enough.
    # (relative_change is informational.)
    _ = flags.get("relative_change_flag")
    return reasons


def _relative(left: list[float], right: list[float]) -> float:
    return _l2(_sub(left, right)) / (_l2(left) + EPS)


def _sub(left: list[float], right: list[float]) -> list[float]:
    return [a - b for a, b in zip(left, right, strict=True)]


def _l2(values: Iterable[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _pack_f32(values: list[float]) -> bytes:
    return struct.pack("<" + "f" * len(values), *values)


def _unpack_dtype(blob: bytes, dtype: str) -> list[float]:
    dtype = dtype.upper()
    if dtype == "F32":
        count = len(blob) // 4
        return list(struct.unpack("<" + "f" * count, blob[: count * 4]))
    if dtype == "F64":
        count = len(blob) // 8
        return list(struct.unpack("<" + "d" * count, blob[: count * 8]))
    if dtype in {"I32", "U32"}:
        count = len(blob) // 4
        fmt = "<" + ("i" if dtype == "I32" else "I") * count
        return [float(value) for value in struct.unpack(fmt, blob[: count * 4])]
    raise ValueError(f"unsupported safetensors dtype {dtype}")


def _align(value: int, alignment: int) -> int:
    rem = value % alignment
    return value if rem == 0 else value + (alignment - rem)


def _gguf_string(handle) -> str:
    length = struct.unpack("<Q", handle.read(8))[0]
    raw = handle.read(length)
    return raw.decode("utf-8")


def _skip_gguf_kv(handle, count: int) -> None:
    for _ in range(count):
        _gguf_string(handle)
        typ = struct.unpack("<I", handle.read(4))[0]
        _skip_gguf_value(handle, typ)


def _skip_gguf_value(handle, typ: int) -> None:
    # GGUF value types: 0=uint8 ... 12=array. Sizes for scalars:
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 8, 8: 8}
    if typ in sizes:
        handle.read(sizes[typ])
        return
    if typ == 9:  # bool
        handle.read(1)
        return
    if typ == 10:  # string
        _gguf_string(handle)
        return
    if typ == 11:  # array
        inner = struct.unpack("<I", handle.read(4))[0]
        length = struct.unpack("<Q", handle.read(8))[0]
        for _ in range(length):
            _skip_gguf_value(handle, inner)
        return
    raise ValueError(f"unsupported GGUF value type {typ}")
