"""Load scheduler settings and job definitions from YAML or built-in defaults."""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sovereign_m.modelguard import yaml_lite
from sovereign_m.scheduler.schedule import Schedule, ScheduleError, parse_schedule

DEFAULTS: dict[str, Any] = {
    # Relative paths: jobs_file is next to the config file, the rest are under the workspace.
    "jobs_file": "jobs.yaml",
    "db": "scheduler/scheduler.sqlite",
    "lock_dir": "scheduler/locks",
    "tick_seconds": 5,
    "tail_bytes": 4096,
    "kill_grace_seconds": 2,
    "status_page": {"host": "loopback", "port": 8490, "allow_nonloopback": False},
    "systemd": {
        "enabled": True,
        "units": ["*"],
        "max_units": 40,
        "log_lines": 5,
        "cache_seconds": 60,
        "command_timeout": 15,
    },
}

JOB_KEYS = {
    "name", "command", "shell", "schedule", "timeout_seconds", "retries",
    "retry_backoff_seconds", "working_dir", "env_allowlist", "lock_group", "enabled",
}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigError(ValueError):
    """The config or jobs file is not usable."""


@dataclass
class Job:
    name: str
    command: list[str] | str
    schedule: Schedule
    shell: bool = False
    timeout_seconds: float = 60.0
    retries: int = 0
    retry_backoff_seconds: float = 5.0
    working_dir: str | None = None
    env_allowlist: list[str] = field(default_factory=list)
    lock_group: str | None = None
    enabled: bool = True


@dataclass
class Settings:
    cfg: dict[str, Any]
    jobs: list[Job]
    workspace: Path

    def _path(self, key: str) -> Path:
        raw = os.path.expandvars(str(self.cfg[key]))
        path = Path(raw)
        return path if path.is_absolute() else self.workspace / path

    @property
    def db_path(self) -> Path:
        return self._path("db")

    @property
    def lock_dir(self) -> Path:
        return self._path("lock_dir")

    def job(self, name: str) -> Job | None:
        return next((j for j in self.jobs if j.name == name), None)


def default_config_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "scheduler" / "config.yaml"
    return path if path.is_file() else None


def load_settings(path: Path | None = None, workspace: Path | None = None) -> Settings:
    from sovereign_m.paths import workspace_dir

    cfg = _copy(DEFAULTS)
    source = Path(path) if path else default_config_path()
    base = source.parent if source else None
    if source is not None:
        try:
            loaded = yaml_lite.load_path(source) or {}
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read config {source}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"config must be a mapping: {source}")
        unknown = set(loaded) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
        _merge(cfg, loaded)
    work = Path(workspace) if workspace else workspace_dir()
    jobs: list[Job] = []
    jobs_file = os.path.expandvars(str(cfg["jobs_file"]))
    jobs_path = Path(jobs_file)
    if not jobs_path.is_absolute() and base is not None:
        jobs_path = base / jobs_path
    if jobs_path.is_file():
        jobs = load_jobs(jobs_path, work)
    elif source is not None and path is not None:
        raise ConfigError(f"jobs file not found: {jobs_path}")
    return Settings(cfg=cfg, jobs=jobs, workspace=work)


def load_jobs(path: Path, workspace: Path) -> list[Job]:
    try:
        loaded = yaml_lite.load_path(path) or {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read jobs {path}: {exc}") from exc
    if not isinstance(loaded, dict) or set(loaded) - {"jobs"}:
        raise ConfigError(f"{path}: expected a single top-level 'jobs' list")
    return parse_jobs(loaded.get("jobs") or [], workspace)


def parse_jobs(raw_jobs: Any, workspace: Path) -> list[Job]:
    if not isinstance(raw_jobs, list):
        raise ConfigError("'jobs' must be a list")
    jobs: list[Job] = []
    seen: set[str] = set()
    for raw in raw_jobs:
        job = parse_job(raw, workspace)
        if job.name in seen:
            raise ConfigError(f"duplicate job name: {job.name}")
        seen.add(job.name)
        jobs.append(job)
    return jobs


def parse_job(raw: Any, workspace: Path) -> Job:
    if not isinstance(raw, dict):
        raise ConfigError(f"a job must be a mapping, got {raw!r}")
    unknown = set(raw) - JOB_KEYS
    if unknown:
        raise ConfigError(f"job {raw.get('name')!r}: unknown keys {', '.join(sorted(unknown))}")
    name = str(raw.get("name") or "")
    if not NAME_RE.match(name):
        raise ConfigError(f"bad job name {name!r} (letters, digits, '_', '.', '-')")
    shell = bool(raw.get("shell", False))
    command = _command(name, raw.get("command"), shell, workspace)
    try:
        schedule = parse_schedule(raw.get("schedule"))
    except ScheduleError as exc:
        raise ConfigError(f"job {name}: {exc}") from exc
    timeout = _number(name, raw, "timeout_seconds", 60.0)
    retries = int(_number(name, raw, "retries", 0))
    backoff = _number(name, raw, "retry_backoff_seconds", 5.0)
    if timeout <= 0:
        raise ConfigError(f"job {name}: timeout_seconds must be positive")
    if not 0 <= retries <= 10:
        raise ConfigError(f"job {name}: retries must be 0-10")
    if backoff < 0:
        raise ConfigError(f"job {name}: retry_backoff_seconds must not be negative")
    group = raw.get("lock_group")
    if group is not None:
        group = str(group)
        if not NAME_RE.match(group):
            raise ConfigError(f"job {name}: bad lock_group {group!r}")
    workdir = raw.get("working_dir")
    if workdir is not None:
        workdir = _substitute(str(workdir), workspace)
    allow = _list(raw.get("env_allowlist"))
    for var in allow:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var):
            raise ConfigError(f"job {name}: bad env name {var!r}")
    return Job(name, command, schedule, shell, timeout, retries, backoff, workdir, allow, group,
               bool(raw.get("enabled", True)))


def _command(name: str, value: Any, shell: bool, workspace: Path) -> list[str] | str:
    if isinstance(value, str):
        if shell:
            return _substitute(value, workspace)
        parts = shlex.split(value)
    elif isinstance(value, list):
        if shell:
            raise ConfigError(f"job {name}: shell jobs take a string command")
        parts = [str(item) for item in value]
    else:
        raise ConfigError(f"job {name}: command is required")
    if not parts:
        raise ConfigError(f"job {name}: command is empty")
    return [_substitute(part, workspace) for part in parts]


def _substitute(text: str, workspace: Path) -> str:
    text = text.replace("{python}", sys.executable).replace("{workspace}", str(workspace))
    return os.path.expandvars(text)


def _number(name: str, raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"job {name}: {key} must be a number")
    return float(value)


def _list(value: Any) -> list[str]:
    if value is None or value == "[]":
        return []
    if not isinstance(value, list):
        raise ConfigError(f"expected a list, got {value!r}")
    return [str(v) for v in value]


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
