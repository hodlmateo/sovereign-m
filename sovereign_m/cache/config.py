"""Load cache settings from YAML or built-in defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign_m.modelguard import yaml_lite

DEFAULTS: dict[str, Any] = {
    "db": "",  # empty means <workspace>/cache/responses.sqlite
    "ttl_seconds": 86400,
    "max_mb": 64,
    "max_entries": 5000,
    "temperature_max": 0.2,
    "default_temperature": None,  # None: a missing temperature is NOT low
    "cache_streams": False,
    "never_cache_routes": [],
    "never_cache_models": [],
    "endpoints": ["/api/chat", "/api/generate", "/v1/chat/completions", "/v1/completions", "/ask-text", "/chat"],
    "markers": [
        "today", "now", "current", "latest", "price", "weather", "my account",
        "my password", "address", "phone", "schedule", "remind",
    ],
    "patterns": [
        r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',
        r'\b(?:\d[ -]?){13,19}\b',
        r'\b\d{3}[-. ]\d{3}[-. ]\d{4}\b',
        r'\(\d{3}\)\s?\d{3}[-. ]\d{4}',
    ],
}


def default_config_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "cache" / "config.yaml"
    return path if path.is_file() else None


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = _copy(DEFAULTS)
    source = path or default_config_path()
    if source is None:
        return cfg
    loaded = yaml_lite.load_path(Path(source)) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config must be a mapping: {source}")
    cfg.update({k: v for k, v in loaded.items() if v is not None})
    return cfg


def with_overrides(config: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults plus a caller's dict. Does not read the data file."""
    cfg = _copy(DEFAULTS)
    cfg.update(config or {})
    return cfg


def default_db_path(cfg: dict[str, Any]) -> Path:
    if cfg.get("db"):
        return Path(str(cfg["db"])).expanduser()
    from sovereign_m.paths import workspace_dir

    return workspace_dir() / "cache" / "responses.sqlite"


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
