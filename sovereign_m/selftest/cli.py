"""Command line for selftest. ``python -m sovereign_m.selftest scan|report``."""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

from sovereign_m.selftest import netinfo, ports, privesc, report as rep, web
from sovereign_m.selftest.config import load_config
from sovereign_m.selftest.scope import Scope, ScopeError

CHECKS = ("ports", "web", "privesc")

USAGE = """
selftest: a read-only self check for machines you own. It looks; it never exploits or changes anything.

  python -m sovereign_m.selftest scan [--check ports,web,privesc] [--config FILE]
                                      [--out-dir DIR] [--no-nmap] [--no-write] [--json]
  python -m sovereign_m.selftest report [--out-dir DIR] [FILE]

Network targets come only from the scope allowlist in the config file (tokens
loopback, lan, vpn, or single addresses you own). A target outside it is
refused. Web probing is GET-only and rate limited. Reports are redacted.
The same verbs work as: python -m sovereign_m selftest ...
""".strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command, rest = args[0], args[1:]
    if command == "scan":
        return _scan(rest)
    if command == "report":
        return _report(rest)
    print(USAGE)
    return 2


def console_main() -> None:
    sys.exit(main())


def _parse(args: list[str], flags: set[str], values: set[str]) -> dict | None:
    out: dict = {"_": []}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in flags:
            out[arg] = True
            i += 1
        elif arg in values:
            if i + 1 >= len(args):
                return None
            out[arg] = args[i + 1]
            i += 2
        elif arg.startswith("-"):
            return None
        else:
            out["_"].append(arg)
            i += 1
    return out


def run_scan(cfg: dict, checks: list[str], allow_nmap: bool = True) -> tuple[list[rep.Finding], dict]:
    """Run the chosen checks. Raises ScopeError for a bad allowlist or service entry."""
    scope = Scope.from_entries(cfg["scope"]["targets"])
    if not scope.allowed:
        raise ScopeError("the allowlist resolved to no addresses on this machine")
    findings: list[rep.Finding] = []
    notes: list[str] = []
    meta: dict = {
        "scope": [f"{ip} ({kind})" for ip, kind in sorted(scope.allowed.items())],
        "checks": checks,
        "firewall": "not checked",
        "scanner": "n/a",
    }
    open_ports: dict[str, list[int]] = {}
    firewall = ports.firewall_state()
    if "ports" in checks:
        open_ports, scanner = ports.scan(scope, cfg["ports"], allow_nmap)
        listeners = ports.read_listeners()
        if not listeners:
            notes.append("`ss` gave no listener list, so bind-address checks were skipped.")
        findings += ports.analyze(scope, cfg["ports"], open_ports, listeners, firewall)
        meta["scanner"] = scanner
        notes.append("The scan runs from this machine, so it shows what is listening and reachable here. "
                     "It cannot prove what another machine on the network is allowed through; test that from a second machine you own.")
        meta["firewall"] = f"{firewall[0]} ({firewall[1]})"
    if "web" in checks:
        wcfg = cfg["web"]
        words = web.clean_wordlist(wcfg.get("wordlist") or []) or web.clean_wordlist(web.SAFE_FALLBACK_WORDLIST)
        targets: dict[tuple[str, int], None] = {}
        for entry in wcfg.get("services") or []:
            for target in scope.expand_service(str(entry)):
                targets[target] = None
        if wcfg.get("auto_discover", True) and open_ports:
            skip = {int(p) for p in wcfg.get("skip_ports") or []}
            for host, found in open_ports.items():
                for port in found:
                    if port not in skip:
                        targets[(scope.require(host), port)] = None
        fetcher = web.Fetcher(scope, float(wcfg["timeout"]), float(wcfg["requests_per_second"]))
        results = [web.probe_service(fetcher, host, port, scope.kind(host), words) for host, port in sorted(targets)]
        sensitive = {str(k): str(v) for k, v in (cfg["ports"].get("sensitive") or {}).items()}
        findings += web.analyze(results, sensitive, firewall[0] == "inactive")
        http_count = sum(1 for r in results if r.http)
        notes.append(f"Web probe: {len(results)} service address(es) tried, {http_count} spoke HTTP, {fetcher.requests} GET requests, {len(words)} paths per service.")
        if "ports" not in checks and not wcfg.get("services"):
            notes.append("Web check had no services: enable the ports check or list services in the config.")
    if "privesc" in checks:
        extra, extra_notes = privesc.run_all(cfg["privesc"])
        findings += extra
        notes += extra_notes
    now = _dt.datetime.now().astimezone()
    meta.update({"date": now.date().isoformat(), "generated": now.isoformat(timespec="seconds"), "notes": notes})
    return findings, meta


def _scan(args: list[str]) -> int:
    parsed = _parse(args, {"--no-nmap", "--no-write", "--json"}, {"--check", "--config", "--out-dir"})
    if parsed is None or parsed["_"]:
        print(USAGE)
        return 2
    checks = list(CHECKS)
    if "--check" in parsed:
        checks = [c.strip() for c in parsed["--check"].split(",") if c.strip()]
        if not checks or any(c not in CHECKS for c in checks):
            print(f"unknown check; choose from: {', '.join(CHECKS)}")
            return 2
    try:
        cfg = load_config(Path(parsed["--config"]) if "--config" in parsed else None)
        findings, meta = run_scan(cfg, checks, allow_nmap="--no-nmap" not in parsed)
    except ScopeError as exc:
        print(f"refused: {exc}")
        return 2
    built = rep.build_report(findings, meta)
    if "--json" in parsed:
        print(json.dumps(built, indent=2, sort_keys=True))
    else:
        print(rep.render_markdown(built))
    if "--no-write" not in parsed:
        out_dir = Path(os.path.expanduser(parsed.get("--out-dir") or cfg["report"]["dir"]))
        if not out_dir.is_absolute():
            from sovereign_m.paths import repo_root

            out_dir = repo_root() / out_dir
        md, js = rep.write_report(built, out_dir)
        print(f"wrote {md}\nwrote {js}", file=sys.stderr)
    return 1 if built["summary"]["high"] else 0


def _report(args: list[str]) -> int:
    parsed = _parse(args, set(), {"--out-dir"})
    if parsed is None or len(parsed["_"]) > 1:
        print(USAGE)
        return 2
    if parsed["_"]:
        path = Path(parsed["_"][0])
    else:
        cfg = load_config()
        out_dir = Path(os.path.expanduser(parsed.get("--out-dir") or cfg["report"]["dir"]))
        if not out_dir.is_absolute():
            from sovereign_m.paths import repo_root

            out_dir = repo_root() / out_dir
        path = rep.latest_report(out_dir)
        if path is None:
            print(f"no report found in {out_dir}")
            return 1
    if not path.is_file():
        print(f"no such report: {path}")
        return 1
    print(rep.redact(path.read_text(encoding="utf-8")))
    return 0
