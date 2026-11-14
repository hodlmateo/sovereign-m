"""Record SHA-256 digests of files, and optional local model listing digests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LOCK_VERSION = 1
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def lock_paths(paths: Iterable[Path], *, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    files = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        files.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    files.sort(key=lambda row: row["path"])
    payload: dict[str, Any] = {
        "version": LOCK_VERSION,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }
    if models:
        payload["models"] = models
    return payload


def write_lock(payload: dict[str, Any], dest: Path) -> None:
    dest = Path(dest)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_lock(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("lock file must be a JSON object")
    return data


def list_ollama_models(client) -> list[dict[str, Any]]:
    """Ask a client for /api/tags. The client must expose a ``_post`` method."""
    data = client._post("/api/tags", {})
    if not data:
        return []
    models = data.get("models")
    if not isinstance(models, list):
        return []
    out: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        digest = item.get("digest") or item.get("id")
        if not name:
            continue
        row = {"name": str(name), "source": "ollama"}
        if digest:
            row["digest"] = str(digest)
        details = item.get("details")
        if isinstance(details, dict):
            for key in ("family", "parameter_size", "quantization_level"):
                if details.get(key):
                    row[key] = details[key]
        out.append(row)
    out.sort(key=lambda row: row["name"])
    return out
