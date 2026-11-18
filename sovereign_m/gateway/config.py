"""Load and validate gateway settings (tiny YAML, ``${VAR}`` expansion)."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from sovereign_m.modelguard import yaml_lite

METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
RESERVED_PREFIX = "/_gateway"
LOOPBACK = ".".join(("127", "0", "0", "1"))
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULTS: dict[str, Any] = {
    "server": {"host": "loopback", "port": 8787, "allow_nonloopback": False},
    "auth": {"keys_file": "", "default_rate_per_min": 120},
    "audit": {"enabled": True, "path": "", "max_bytes": 5_242_880, "keep": 5, "query": "strip"},
    "middleware": [],
    "routes": [],
}


class ConfigError(ValueError):
    pass


@dataclass
class Route:
    name: str
    prefix: str
    upstream: str
    scheme: str = "http"
    host: str = ""
    port: int = 0
    base_path: str = ""
    methods: tuple[str, ...] = ("GET",)
    allow: list[tuple[str | None, str]] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    timeout: float = 30.0
    max_body_bytes: int = 1_048_576
    strip_prefix: bool = True
    websocket: bool = False
    allow_admin: bool = False
    allow_docs: bool = False
    health_path: str = "/"
    upstream_auth: dict[str, str] | None = None
    rate_per_min: int | None = None
    middleware: list[Any] = field(default_factory=list)
    use_global_middleware: bool = True
    disabled_reason: str = ""

    @property
    def enabled(self) -> bool:
        return not self.disabled_reason


@dataclass
class Config:
    host: str
    port: int
    allow_nonloopback: bool
    keys_file: str
    default_rate_per_min: int
    audit: dict[str, Any]
    middleware: list[Any]
    routes: list[Route]
    warnings: list[str] = field(default_factory=list)


def default_config_path() -> Path | None:
    from sovereign_m.paths import data_dir

    path = data_dir() / "gateway" / "config.yaml"
    return path if path.is_file() else None


def load_config(path: Path | str | None = None, env: Mapping[str, str] | None = None) -> Config:
    source = Path(path) if path else default_config_path()
    raw: Any = {}
    if source is not None:
        if not source.is_file():
            raise ConfigError(f"config not found: {source}")
        raw = yaml_lite.load_path(source) or {}
    return parse_config(raw, env)


def parse_config(raw: Any, env: Mapping[str, str] | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    env = os.environ if env is None else env
    merged = _merge(_copy(DEFAULTS), _expand(raw, env))
    errors: list[str] = []
    server = _section(merged, "server", errors)
    auth = _section(merged, "auth", errors)
    audit = _section(merged, "audit", errors)
    host = str(server.get("host", "loopback")).strip()
    port = _int(server.get("port"), "server.port", errors, 0, 65535)
    rate = _int(auth.get("default_rate_per_min"), "auth.default_rate_per_min", errors, 0, 10_000_000)
    if audit.get("query") not in {"strip", "redact"}:
        errors.append("audit.query must be strip or redact")
    for key in ("max_bytes", "keep"):
        audit[key] = _int(audit.get(key), f"audit.{key}", errors, 1, 1 << 40)
    audit["enabled"] = _truthy(audit.get("enabled"))
    middleware = _middleware_list(merged.get("middleware"), "middleware", errors)
    routes: list[Route] = []
    raw_routes = merged.get("routes")
    if raw_routes in (None, "[]"):
        raw_routes = []
    if not isinstance(raw_routes, list):
        errors.append("routes must be a list")
        raw_routes = []
    warnings: list[str] = []
    for index, entry in enumerate(raw_routes):
        route = _route(entry, index, errors, warnings)
        if route is not None:
            routes.append(route)
    seen_names: set[str] = set()
    seen_prefixes: set[str] = set()
    for route in routes:
        if route.name in seen_names:
            errors.append(f"duplicate route name: {route.name}")
        if route.prefix in seen_prefixes:
            errors.append(f"duplicate route prefix: {route.prefix}")
        seen_names.add(route.name)
        seen_prefixes.add(route.prefix)
    if errors:
        raise ConfigError("; ".join(errors))
    return Config(
        host=host,
        port=port,
        allow_nonloopback=_truthy(server.get("allow_nonloopback")),
        keys_file=str(auth.get("keys_file") or ""),
        default_rate_per_min=rate,
        audit=audit,
        middleware=middleware,
        routes=routes,
        warnings=warnings,
    )


def _route(entry: Any, index: int, errors: list[str], warnings: list[str]) -> Route | None:
    label = f"routes[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{label} must be a mapping")
        return None
    name = str(entry.get("name") or "")
    if not _NAME.match(name):
        errors.append(f"{label}: name must be letters, digits, dot, dash or underscore")
        return None
    label = f"route {name}"
    problems = len(errors)
    prefix = str(entry.get("prefix") or "")
    if not prefix.startswith("/"):
        errors.append(f"{label}: prefix must start with /")
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    if prefix.startswith(RESERVED_PREFIX):
        errors.append(f"{label}: prefix may not start with {RESERVED_PREFIX}")
    methods = tuple(str(m).upper() for m in as_list(entry.get("methods"), label, errors)) or ("GET",)
    for method in methods:
        if method not in METHODS:
            errors.append(f"{label}: unknown method {method}")
    allow: list[tuple[str | None, str]] = []
    for item in as_list(entry.get("allow"), label, errors):
        parts = str(item).split()
        method = None
        if len(parts) == 2 and parts[0].upper() in METHODS:
            method, parts = parts[0].upper(), parts[1:]
        if len(parts) != 1 or not (parts[0].startswith("/") or parts[0] == "*"):
            errors.append(f"{label}: bad allow entry: {item}")
            continue
        allow.append((method, parts[0]))
    deny = [str(p) for p in as_list(entry.get("deny"), label, errors)]
    for pattern in deny:
        if not pattern.startswith("/"):
            errors.append(f"{label}: deny pattern must start with /: {pattern}")
    if not allow:
        warnings.append(f"route {name}: empty allow list, every path will be refused")
    if _truthy(entry.get("websocket")):
        errors.append(f"{label}: websocket proxying is not implemented; leave websocket: false")
    health = str(entry.get("health_path") or "/")
    if not health.startswith("/"):
        errors.append(f"{label}: health_path must start with /")
    route = Route(
        name=name,
        prefix=prefix,
        upstream=str(entry.get("upstream") or ""),
        methods=methods,
        allow=allow,
        deny=deny,
        timeout=float(_int(entry.get("timeout", 30), f"{label}: timeout", errors, 1, 3600)),
        max_body_bytes=_int(entry.get("max_body_bytes", 1_048_576), f"{label}: max_body_bytes", errors, 0, 1 << 40),
        strip_prefix=_truthy(entry.get("strip_prefix", True)),
        allow_admin=_truthy(entry.get("allow_admin", False)),
        allow_docs=_truthy(entry.get("allow_docs", False)),
        health_path=health,
        middleware=_middleware_list(entry.get("middleware"), f"{label}: middleware", errors),
        use_global_middleware=_truthy(entry.get("use_global_middleware", True)),
    )
    if entry.get("rate_per_min") not in (None, ""):
        route.rate_per_min = _int(entry.get("rate_per_min"), f"{label}: rate_per_min", errors, 0, 10_000_000)
    auth = entry.get("upstream_auth")
    if isinstance(auth, dict):
        key_file = str(auth.get("key_file") or "")
        if not key_file:
            errors.append(f"{label}: upstream_auth needs key_file")
        scheme = auth.get("scheme")
        route.upstream_auth = {
            "header": str(auth.get("header") or "Authorization"),
            "scheme": "Bearer" if scheme is None else str(scheme),
            "key_file": key_file,
        }
    elif auth not in (None, ""):
        errors.append(f"{label}: upstream_auth must be a mapping")
    _parse_upstream(route, errors)
    return route if len(errors) == problems else None


def _parse_upstream(route: Route, errors: list[str]) -> None:
    if "${" in route.upstream:
        names = ", ".join(sorted({m.group(1) for m in _VAR.finditer(route.upstream)}))
        route.disabled_reason = f"set {names}" if names else "upstream has an unresolved variable"
        return
    try:
        parts = urlsplit(route.upstream)
        port = parts.port
    except ValueError:
        errors.append(f"route {route.name}: bad upstream URL")
        return
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        errors.append(f"route {route.name}: upstream must be http(s)://host:port")
        return
    if parts.username or parts.password or parts.query or parts.fragment:
        errors.append(f"route {route.name}: upstream may not carry credentials, query or fragment")
        return
    route.scheme = parts.scheme
    route.host = parts.hostname
    route.port = port or (443 if parts.scheme == "https" else 80)
    route.base_path = parts.path.rstrip("/")


def host_is_loopback(host: str) -> bool:
    """True when every address the host name resolves to is loopback."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {info[4][0].split("%")[0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a).is_loopback for a in addrs)


def as_list(value: Any, label: str, errors: list[str]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [p.strip().strip("'\"") for p in text.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    errors.append(f"{label}: expected a list")
    return []


def _middleware_list(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in as_list(value, label, errors):
        if isinstance(item, str):
            item = {"use": item}
        if not isinstance(item, dict) or ":" not in str(item.get("use") or ""):
            errors.append(f"{label}: each entry needs use: module:attr")
            continue
        config = item.get("config")
        if config in (None, "{}"):
            config = {}
        if not isinstance(config, dict):
            errors.append(f"{label}: config must be a mapping")
            continue
        out.append({"use": str(item["use"]), "config": config})
    return out


def _section(merged: dict, key: str, errors: list[str]) -> dict:
    value = merged.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be a mapping")
        return {}
    return value


def _int(value: Any, label: str, errors: list[str], low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        errors.append(f"{label}: expected a whole number")
        return low
    if not low <= number <= high:
        errors.append(f"{label}: must be between {low} and {high}")
    return number


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on"}
    return bool(value)


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def sub(match: re.Match) -> str:
            found = env.get(match.group(1))
            if found:
                return found
            return match.group(2) if match.group(2) is not None else match.group(0)

        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def _merge(base: dict, incoming: dict) -> dict:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
