"""Load modelguard settings from a YAML file or built-in defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign_m.modelguard import yaml_lite


DEFAULTS: dict[str, Any] = {
    "diff": {
        "relative_change_flag": 0.15,
        "concentration_flag": 6.0,
        "top_row_share_flag": 0.45,
        "neuron_count_flag": 16,
        "neuron_share_flag": 0.5,
        "neuron_top_k": 16,
        "depth_weighting": True,
        "late_layer_weight": 1.5,
    },
    "probe": {
        "endpoint": "",
        "kind": "ollama",
        "model": "",
        "timeout": 30,
        "change_flag": 0.6,
        "system": "Reply in one short sentence.",
        "baseline_user": "Say hello in one short sentence.",
    },
}


def default_config_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "modelguard" / "config.yaml"
    return path if path.is_file() else None


def default_probes_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "modelguard" / "probes.yaml"
    return path if path.is_file() else None


def default_cmdguard_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "modelguard" / "cmdguard.yaml"
    return path if path.is_file() else None


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = _deep_copy(DEFAULTS)
    source = path or default_config_path()
    if source is None:
        return cfg
    loaded = yaml_lite.load_path(source) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config must be a mapping: {source}")
    _merge(cfg, loaded)
    return cfg


def _merge(base: dict, incoming: dict) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
