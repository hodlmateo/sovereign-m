"""Load selftest settings from YAML or built-in defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign_m.modelguard import yaml_lite

DEFAULTS: dict[str, Any] = {
    "scope": {"targets": ["loopback", "lan", "vpn"]},
    "report": {"dir": "var/selftest"},
    "ports": {
        "range": "1-65535",
        "use_nmap": True,
        "nmap_timing": 3,
        "connect_timeout": 0.5,
        "workers": 100,
        "expected_wide": [22],
        "sensitive": {},
    },
    "web": {
        "auto_discover": True,
        "services": [],
        "skip_ports": [22, 53, 139, 445],
        "requests_per_second": 15,
        "timeout": 3,
        "wordlist": [],
    },
    "privesc": {
        "suid_roots": ["/usr", "/opt", "/bin", "/sbin", "/srv"],
        "suid_known": [],
        "suid_risky": [],
        "unit_dirs": [],
        "secret_prune": [],
        "secret_max_depth": 6,
        "run_linpeas_if_present": True,
    },
}


def default_config_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "selftest" / "config.yaml"
    return path if path.is_file() else None


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = _copy(DEFAULTS)
    source = path or default_config_path()
    if source is None:
        return cfg
    loaded = yaml_lite.load_path(Path(source)) or {}
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


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
