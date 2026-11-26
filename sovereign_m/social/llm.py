"""The local model, through the existing routines helper (Qwen3.6-35B-A3B first, qwen2.5:14b fallback).

m_llm.py never prints its key and skips the model when the heat flag is up (returns empty text).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from sovereign_m.social.brands import hub_root


def chat(system: str, user: str, max_tokens: int = 900, hub: Path | None = None) -> tuple[str, str]:
    """Return (text, model_used). Empty text means unavailable: callers must not invent content."""
    path = str((hub or hub_root()) / "board" / "routines" / "local")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        m = importlib.import_module("m_llm")
        text, model = m.chat(system, user, max_tokens=max_tokens)
        return (text or "").strip(), str(model)
    except Exception as exc:  # noqa: BLE001 - any failure is "model unavailable"
        return "", f"unavailable: {type(exc).__name__}"
