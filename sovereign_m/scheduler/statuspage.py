"""Read-only status page: GET / (HTML), GET /api/status (JSON), GET /health.

Meant to sit behind the gateway as a route. It binds to the loopback address
unless an explicit setting says otherwise, and has no endpoint that changes
anything. Links are relative so a route prefix works.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from sovereign_m.scheduler import schedule as sched
from sovereign_m.scheduler.config import Settings
from sovereign_m.scheduler.store import Store
from sovereign_m.scheduler.systemd import ExternalTimer, SystemdReader, import_timers

LOOPBACK = ".".join(("127", "0", "0", "1"))
REFRESH_SECONDS = 30
RECENT_LIMIT = 25

ExternalProvider = Callable[[], tuple[list[ExternalTimer], str | None]]


class BindRefused(Exception):
    """The requested bind address is not loopback and was not explicitly allowed."""


def resolve_bind(host: str, allow_nonloopback: bool = False) -> tuple[str, str | None]:
    """Return (address, warning). Names other than loopback/localhost are never resolved."""
    text = (host or "loopback").strip().lower()
    if text in {"loopback", "localhost"}:
        return LOOPBACK, None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise BindRefused(f"refusing host {host!r}: use 'loopback' or a literal address") from None
    if addr.is_loopback:
        return str(addr), None
    if not allow_nonloopback:
        raise BindRefused(
            f"refusing to bind {addr}: not loopback. Pass --allow-nonloopback (or set "
            "status_page.allow_nonloopback) only if something else controls who can connect.")
    return str(addr), (f"WARNING: binding {addr}, which is not loopback. The page has no login; "
                       "anyone who can reach it can read job names, results and log tails.")


def clean_client(value: str | None) -> str | None:
    """X-M-Client comes from the gateway and is shown, never trusted for decisions."""
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._:@+-]", "", value)[:64]
    return cleaned or None


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _attempt_view(row: dict[str, Any]) -> dict[str, Any]:
    end = row.get("end")
    return {
        "job": row["job"], "attempt": row["attempt"], "status": row["status"],
        "start": _iso(row["start"]), "end": _iso(end),
        "duration_seconds": round(end - row["start"], 2) if end else None,
        "exit_code": row["exit_code"],
    }


def build_status(settings: Settings, store: Store, now: float,
                 external: ExternalProvider | None = None, client: str | None = None) -> dict[str, Any]:
    when = datetime.fromtimestamp(now)
    live = {r["job"]: r for r in store.running() if _pid_alive(r["pid"])}
    jobs = []
    for job in settings.jobs:
        last_start_epoch = store.last_run_start(job.name)
        last_start = datetime.fromtimestamp(last_start_epoch) if last_start_epoch is not None else None
        nxt = sched.next_run(job.schedule, last_start, when) if job.enabled else None
        finished = store.last_finished(job.name)
        jobs.append({
            "name": job.name, "schedule": job.schedule.describe(), "enabled": job.enabled,
            "lock_group": job.lock_group, "running": job.name in live,
            "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
            "due": bool(nxt and nxt <= when),
            "last": _attempt_view(finished) if finished else None,
        })
    ext_rows: list[dict[str, Any]] = []
    ext_error = None
    if external is not None:
        timers, ext_error = external()
        ext_rows = [{
            "unit": t.unit, "activates": t.activates, "next": _iso(t.next_run), "last": _iso(t.last_run),
            "result": t.result, "exit_status": t.exit_status, "active_state": t.active_state,
            "logs": t.logs, "error": t.error,
        } for t in timers]
    return {
        "generated": _iso(now), "client": client, "read_only": True,
        "running": [_attempt_view(r) for r in live.values()],
        "jobs": jobs,
        "recent": [_attempt_view(r) for r in store.recent(RECENT_LIMIT) if r["status"] != "running"],
        "external": ext_rows, "external_error": ext_error,
    }


# -- HTML ---------------------------------------------------------------------------

_CSS = (
    "body{font:14px/1.45 system-ui,sans-serif;margin:1.5rem;background:#fafafa;color:#1b1b1b}"
    "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.6rem}"
    "table{border-collapse:collapse;width:100%;background:#fff}"
    "th,td{border:1px solid #d8d8d8;padding:.3rem .5rem;text-align:left;vertical-align:top}"
    "th{background:#efefef}.ok{color:#116611}.failed,.timeout{color:#a01515;font-weight:600}"
    ".skipped-locked,.running{color:#8a5a00}.muted{color:#666}pre{margin:.2rem 0;white-space:pre-wrap}"
)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _status_cell(status: str | None) -> str:
    if not status:
        return '<span class="muted">-</span>'
    cls = re.sub(r"[^a-z-]", "", status.lower())
    return f'<span class="{cls}">{_e(status)}</span>'


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def render_html(status: dict[str, Any]) -> str:
    running = [[_e(r["job"]), _e(r["attempt"]), _e(r["start"])] for r in status["running"]]
    next_rows = []
    for j in status["jobs"]:
        last = j["last"]
        if not j["enabled"]:
            nxt = "disabled"
        elif j["running"]:
            nxt = "running"
        elif j["due"]:
            nxt = "due now"
        else:
            nxt = j["next_run"] or "never"
        next_rows.append([
            _e(j["name"]), _e(j["schedule"]), _e(nxt), _e(j["lock_group"] or ""),
            _status_cell(last["status"] if last else None)
            + (f" <span class=\"muted\">{_e(last['start'])}</span>" if last else ""),
        ])
    recent = [[
        _e(r["start"]), _e(r["job"]), _e(r["attempt"]), _status_cell(r["status"]),
        _e("" if r["duration_seconds"] is None else f"{r['duration_seconds']}s"),
        _e("" if r["exit_code"] is None else r["exit_code"]),
    ] for r in status["recent"]]
    ext = []
    for t in status["external"]:
        logs = "<pre>" + _e("\n".join(t["logs"])) + "</pre>" if t["logs"] else ""
        detail = _e(t["unit"]) + (f"<details><summary>log</summary>{logs}</details>" if logs else "")
        result = _e(t["result"] or t["error"] or "")
        if t["exit_status"] not in (None, "", "0"):
            result += _e(f" (exit {t['exit_status']})")
        ext.append([detail, _e(t["next"] or "n/a"), _e(t["last"] or "n/a"), result])
    ext_note = (f'<p class="muted">systemd import: {_e(status["external_error"])}</p>'
                if status["external_error"] else "")
    client = f' <span class="muted">viewer: {_e(status["client"])}</span>' if status["client"] else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>scheduler</title><style>{_CSS}</style></head><body>"
        f"<h1>scheduler <span class=\"muted\">(read-only)</span></h1>"
        f"<p class=\"muted\">Updated {_e(status['generated'])}, refreshes every {REFRESH_SECONDS}s."
        f"{client} <a href=\"api/status\">json</a></p>"
        "<h2>Running / Next up</h2>"
        + _table(["Running job", "Attempt", "Started"], running, "Nothing is running.")
        + _table(["Job", "Schedule", "Next", "Lock group", "Last result"], next_rows, "No jobs are defined.")
        + "<h2>Recent runs</h2>"
        + _table(["Started", "Job", "Attempt", "Status", "Duration", "Exit"], recent, "No runs recorded yet.")
        + "<h2>External systemd timers</h2>"
        + ext_note
        + _table(["Timer", "Next", "Last", "Result"], ext, "No external timers imported.")
        + "</body></html>"
    )


# -- the app and server ---------------------------------------------------------------

class StatusApp:
    def __init__(self, settings: Settings, store: Store | None = None,
                 external: ExternalProvider | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.clock = clock
        self._external = external
        self._cache: tuple[float, tuple[list[ExternalTimer], str | None]] | None = None
        self._lock = threading.Lock()

    @classmethod
    def with_systemd(cls, settings: Settings, **kw: Any) -> "StatusApp":
        scfg = settings.cfg["systemd"]
        app = cls(settings, **kw)
        if scfg.get("enabled") and app._external is None:
            reader = SystemdReader(timeout=float(scfg["command_timeout"]))
            app._external = lambda: import_timers(
                reader, scfg.get("units"), int(scfg["max_units"]), int(scfg["log_lines"]))
        return app

    def _external_cached(self) -> tuple[list[ExternalTimer], str | None]:
        ttl = float(self.settings.cfg["systemd"]["cache_seconds"])
        with self._lock:
            now = self.clock()
            if self._cache and now - self._cache[0] < ttl:
                return self._cache[1]
            value = self._external() if self._external else ([], None)
            self._cache = (now, value)
            return value

    def status(self, client: str | None = None) -> dict[str, Any]:
        provider = self._external_cached if self._external else None
        return build_status(self.settings, self.store, self.clock(), provider, client)


def make_handler(app: StatusApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "scheduler-status"
        sys_version = ""

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str, head_only: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _get(self, head_only: bool) -> None:
            path = self.path.split("?", 1)[0]
            client = clean_client(self.headers.get("X-M-Client"))
            if path in {"/", "/index.html"}:
                body = render_html(app.status(client)).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8", head_only)
            elif path == "/api/status":
                body = json.dumps(app.status(client), indent=2, sort_keys=True).encode("utf-8")
                self._send(200, body, "application/json", head_only)
            elif path == "/health":
                self._send(200, b'{"ok": true}', "application/json", head_only)
            else:
                self._send(404, b'{"error": "not found"}', "application/json", head_only)

        def do_GET(self) -> None:
            self._get(False)

        def do_HEAD(self) -> None:
            self._get(True)

        def _refuse(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    return Handler


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_server(app: StatusApp, host: str = "loopback", port: int = 0,
                allow_nonloopback: bool = False) -> tuple[ThreadingHTTPServer, str | None]:
    address, warning = resolve_bind(host, allow_nonloopback)
    cls = _V6Server if ipaddress.ip_address(address).version == 6 else ThreadingHTTPServer
    server = cls((address, port), make_handler(app))
    server.daemon_threads = True
    return server, warning
