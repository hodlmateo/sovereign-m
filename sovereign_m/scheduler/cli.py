"""Command line for the scheduler. ``python -m sovereign_m.scheduler VERB``."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from sovereign_m.scheduler.config import ConfigError, Settings, load_settings
from sovereign_m.scheduler.runner import Runner
from sovereign_m.scheduler.statuspage import BindRefused, StatusApp, make_server
from sovereign_m.scheduler.systemd import SystemdReader, import_timers

USAGE = """
scheduler: a job runner with a read-only status page.

  python -m sovereign_m.scheduler run [--once] [--config PATH]
  python -m sovereign_m.scheduler run-job NAME [--force] [--config PATH]
  python -m sovereign_m.scheduler list [--json] [--no-external] [--config PATH]
  python -m sovereign_m.scheduler import-timers [--json] [--config PATH]
  python -m sovereign_m.scheduler status-page [--host loopback] [--port N]
                                              [--allow-nonloopback] [--config PATH]

systemd is only read (list-timers, show, status, list-units, and journal reads).
The status page has no actions. The same verbs work as: python -m sovereign_m scheduler ...
""".strip()


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scheduler", add_help=False)
    sub = parser.add_subparsers(dest="verb")

    def add(name: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, add_help=False)
        p.add_argument("--config")
        return p

    p = add("run")
    p.add_argument("--once", action="store_true")
    p = add("run-job")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")
    p = add("list")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-external", action="store_true")
    p = add("import-timers")
    p.add_argument("--json", action="store_true")
    p = add("status-page")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--allow-nonloopback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    parser = _parser()
    try:
        ns = parser.parse_args(args)
    except (_UsageError, SystemExit):
        print(USAGE)
        return 2
    if not ns.verb:
        print(USAGE)
        return 2
    try:
        settings = load_settings(Path(ns.config) if ns.config else None)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    handler = {
        "run": _run, "run-job": _run_job, "list": _list,
        "import-timers": _import_timers, "status-page": _status_page,
    }[ns.verb]
    return handler(settings, ns)


def console_main() -> None:
    sys.exit(main())


def _fmt(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else "-"


def _run(settings: Settings, ns: argparse.Namespace) -> int:
    runner = Runner(settings)
    if ns.once:
        names = runner.run_once()
        print(f"ran {len(names)} due job(s): {', '.join(names) or 'none'}")
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    enabled = sum(1 for j in settings.jobs if j.enabled)
    print(f"scheduler running: {enabled} enabled job(s), db {settings.db_path}", flush=True)
    runner.loop(stop)
    print("scheduler stopped", flush=True)
    return 0


def _run_job(settings: Settings, ns: argparse.Namespace) -> int:
    job = settings.job(ns.name)
    if job is None:
        print(f"no such job: {ns.name}", file=sys.stderr)
        return 2
    if not job.enabled and not ns.force:
        print(f"job {ns.name} is disabled (use --force to run it anyway)", file=sys.stderr)
        return 2
    attempts = Runner(settings).run_job(job)
    for a in attempts:
        print(f"{a.job} attempt {a.attempt}: {a.status} exit={a.exit_code if a.exit_code is not None else '-'}")
    last = attempts[-1]
    if last.excerpt:
        print(last.excerpt.rstrip())
    if last.status == "skipped-locked":
        return 3
    return 0 if last.status == "ok" else 1


def _external(settings: Settings) -> tuple[list, str | None]:
    scfg = settings.cfg["systemd"]
    reader = SystemdReader(timeout=float(scfg["command_timeout"]))
    return import_timers(reader, scfg.get("units"), int(scfg["max_units"]), int(scfg["log_lines"]))


def _list(settings: Settings, ns: argparse.Namespace) -> int:
    app = StatusApp(settings, external=None if ns.no_external or not settings.cfg["systemd"]["enabled"]
                    else (lambda: _external(settings)))
    status = app.status()
    if ns.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    rows = [("NAME", "KIND", "SCHEDULE", "NEXT", "LAST", "RESULT")]
    for j in status["jobs"]:
        last = j["last"]
        nxt = "disabled" if not j["enabled"] else ("running" if j["running"] else
                                                   ("due" if j["due"] else (j["next_run"] or "-")))
        rows.append((j["name"], "job", j["schedule"], nxt, last["start"] if last else "-",
                     last["status"] if last else "-"))
    for t in status["external"]:
        result = t["result"] or t["error"] or "-"
        rows.append((t["unit"], "external", "systemd", t["next"] or "-", t["last"] or "-", result))
    widths = [max(len(str(r[i])) for r in rows) for i in range(6)]
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
    if status["external_error"]:
        print(f"(systemd import: {status['external_error']})", file=sys.stderr)
    return 0


def _import_timers(settings: Settings, ns: argparse.Namespace) -> int:
    timers, error = _external(settings)
    if ns.json:
        print(json.dumps({"timers": [t.as_dict() for t in timers], "error": error}, indent=2, sort_keys=True))
        return 0 if error is None else 1
    if error:
        print(f"import failed: {error}", file=sys.stderr)
        return 1
    for t in timers:
        print(f"{t.unit}  next={_fmt(t.next_run)}  last={_fmt(t.last_run)}  "
              f"result={t.result or '-'}  exit={t.exit_status or '-'}")
        for line in t.logs:
            print(f"    {line}")
    print(f"{len(timers)} timer(s), read-only")
    return 0


def _status_page(settings: Settings, ns: argparse.Namespace) -> int:
    page = settings.cfg["status_page"]
    host = ns.host or page["host"]
    port = ns.port if ns.port is not None else int(page["port"])
    allow = bool(ns.allow_nonloopback or page.get("allow_nonloopback"))
    app = StatusApp.with_systemd(settings)
    try:
        server, warning = make_server(app, host, port, allow)
    except BindRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if warning:
        print(warning, file=sys.stderr)
    shown = server.server_address
    print(f"status page on http://{shown[0]}:{shown[1]}/ (read-only)", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stop.wait()
    server.shutdown()
    server.server_close()
    return 0
