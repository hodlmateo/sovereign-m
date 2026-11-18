"""Command line: ``python -m sovereign_m.gateway serve|check-config|keys``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sovereign_m.gateway.audit import AuditLog
from sovereign_m.gateway.config import Config, ConfigError, host_is_loopback, load_config
from sovereign_m.gateway.core import Gateway
from sovereign_m.gateway.keys import KeyFileError, KeyStore, parse_rate
from sovereign_m.gateway.server import BindRefused, make_server

USAGE = """
gateway: one authenticated front door for local services.

  python -m sovereign_m.gateway serve [--config PATH] [--host HOST] [--port N] [--allow-nonloopback]
  python -m sovereign_m.gateway check-config [--config PATH] [--strict]
  python -m sovereign_m.gateway keys add CLIENT [--scopes route1,route2] [--rate N/min]
  python -m sovereign_m.gateway keys list
  python -m sovereign_m.gateway keys revoke CLIENT
  (keys verbs also take --config PATH and --keys-file PATH)

Binds the loopback address unless you say otherwise. See docs/gateway.md.
The same verbs work as: python -m sovereign_m gateway ...
""".strip()


def _workspace_file(name: str) -> Path:
    from sovereign_m.paths import workspace_dir

    return workspace_dir() / "gateway" / name


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path)))


def keys_path(cfg: Config | None, override: str | None = None) -> Path:
    if override:
        return _expand(override)
    if cfg is not None and cfg.keys_file:
        return _expand(cfg.keys_file)
    return _workspace_file("keys.json")


def audit_from(cfg: Config) -> AuditLog:
    a = cfg.audit
    path = _expand(a["path"]) if a.get("path") else _workspace_file("audit.jsonl")
    return AuditLog(path, int(a["max_bytes"]), int(a["keep"]), a["query"], bool(a["enabled"]))


def build_gateway(cfg: Config, keys_file: str | None = None) -> Gateway:
    store = KeyStore(keys_path(cfg, keys_file))
    store.load()
    return Gateway(cfg, store, audit_from(cfg))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    parser = argparse.ArgumentParser(prog="python -m sovereign_m.gateway", add_help=False)
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", add_help=False)
    serve.add_argument("--config")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--keys-file")
    serve.add_argument("--allow-nonloopback", action="store_true")
    check = sub.add_parser("check-config", add_help=False)
    check.add_argument("--config")
    check.add_argument("--strict", action="store_true")
    keys = sub.add_parser("keys", add_help=False)
    keys.add_argument("action", choices=["add", "list", "revoke"])
    keys.add_argument("client", nargs="?")
    keys.add_argument("--scopes")
    keys.add_argument("--rate")
    keys.add_argument("--config")
    keys.add_argument("--keys-file")
    try:
        ns = parser.parse_args(args)
    except SystemExit:
        print(USAGE)
        return 2
    if ns.command == "serve":
        return _serve(ns)
    if ns.command == "check-config":
        return _check(ns)
    if ns.command == "keys":
        return _keys(ns)
    print(USAGE)
    return 2


def console_main() -> None:
    sys.exit(main())


def _load(path: str | None) -> Config | None:
    try:
        return load_config(path)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _serve(ns: argparse.Namespace) -> int:
    cfg = _load(ns.config)
    if cfg is None:
        return 2
    try:
        gateway = build_gateway(cfg, ns.keys_file)
        server, warnings = make_server(cfg, gateway, ns.host, ns.port,
                                       True if ns.allow_nonloopback else None)
    except (BindRefused, KeyFileError, RuntimeError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for line in cfg.warnings + warnings:
        print(line, file=sys.stderr)
    if not gateway.keys.list():
        print("no client keys yet: every request gets 401. Create one with: keys add CLIENT", file=sys.stderr)
    host, port = server.server_address[:2]
    print(f"gateway listening on {host}:{port} with {len(gateway.routes)} route(s)", file=sys.stderr)
    for route in cfg.routes:
        if not route.enabled:
            print(f"  route {route.name}: disabled ({route.disabled_reason})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.audit.close()
    return 0


def _check(ns: argparse.Namespace) -> int:
    cfg = _load(ns.config)
    if cfg is None:
        return 1
    print(f"listen: {cfg.host} port {cfg.port}" + ("  (non-loopback allowed)" if cfg.allow_nonloopback else ""))
    flagged = 0
    for route in cfg.routes:
        print(f"route {route.name}: {route.prefix} -> {route.upstream or '(none)'}")
        print(f"  methods {','.join(route.methods)}  allow {len(route.allow)}  deny {len(route.deny)}"
              f"  timeout {route.timeout:g}s  max_body {route.max_body_bytes}"
              f"  strip_prefix {str(route.strip_prefix).lower()}"
              + ("  allow_admin" if route.allow_admin else ""))
        if not route.enabled:
            print(f"  DISABLED: {route.disabled_reason}")
            continue
        if not host_is_loopback(route.host):
            flagged += 1
            print(f"  WARNING: upstream host {route.host} is not loopback")
        if route.upstream_auth:
            kf = _expand(route.upstream_auth["key_file"])
            if not kf.is_file():
                print(f"  WARNING: upstream key file not found: {kf}")
            elif kf.stat().st_mode & 0o077:
                print(f"  WARNING: upstream key file is readable by group or others: {kf}")
    for line in cfg.warnings:
        print(f"WARNING: {line}")
    store = keys_path(cfg)
    try:
        KeyStore(store).load()
    except KeyFileError as exc:
        print(f"WARNING: {exc}")
    print(f"{len(cfg.routes)} route(s), {sum(1 for r in cfg.routes if r.enabled)} enabled, "
          f"{flagged} with a non-loopback upstream")
    return 1 if ns.strict and (flagged or cfg.warnings) else 0


def _keys(ns: argparse.Namespace) -> int:
    cfg = None
    if ns.config or ns.action == "add":
        cfg = _load(ns.config) if ns.config else _try_default()
        if ns.config and cfg is None:
            return 2
    store = KeyStore(keys_path(cfg, ns.keys_file))
    try:
        if ns.action == "list":
            rows = store.list()
            if not rows:
                print("no clients")
            for row in rows:
                rate = row["rate_per_min"]
                print(f"{row['client']}  scopes={','.join(row['scopes'])}  "
                      f"rate={'default' if rate is None else str(rate) + '/min'}  created={row['created']}")
            return 0
        if not ns.client:
            print(USAGE)
            return 2
        if ns.action == "revoke":
            if store.revoke(ns.client):
                print(f"revoked {ns.client}")
                return 0
            print(f"no such client: {ns.client}")
            return 1
        scopes = [s for s in (ns.scopes or "").split(",") if s.strip()] or None
        rate = parse_rate(ns.rate) if ns.rate else None
        if cfg is not None and scopes:
            known = {r.name for r in cfg.routes}
            for scope in scopes:
                if scope != "*" and scope not in known:
                    print(f"note: scope {scope} is not a route in the config", file=sys.stderr)
        key = store.add(ns.client, scopes, rate)
    except (KeyFileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Key for client " + ns.client + " (shown once, not stored; save it now):", file=sys.stderr)
    print(key)
    return 0


def _try_default() -> Config | None:
    try:
        return load_config(None)
    except (ConfigError, OSError, ValueError):
        return None

