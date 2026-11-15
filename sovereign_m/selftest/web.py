"""Check 2: a small, polite, GET-only path probe of allowlisted local HTTP services.

What it never does: send POST/PUT/DELETE (or any method but GET), follow
redirects, keep response bodies, or request a path whose name sounds like an
action (reset, restart, send, approve ...). Whether an endpoint changes state
is inferred only from three sources: an OpenAPI document the service itself
publishes, a 405 answer to GET, or a known banner such as Ollama's.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import time
from dataclasses import dataclass, field

from sovereign_m.selftest.report import Finding, redact
from sovereign_m.selftest.scope import Scope

# Path words that sound like actions. A GET on a badly written service could run them.
UNSAFE_WORDS = {
    "reset", "restart", "reboot", "stop", "start", "kill", "shutdown", "halt", "pause", "resume",
    "send", "approve", "reject", "run", "exec", "execute", "wake", "delete", "remove", "clear",
    "upload", "import", "train", "finish", "deploy", "update", "write", "save", "create", "pull",
    "push", "trigger", "submit", "apply", "install", "uninstall", "unlock", "cancel", "flush",
    "purge", "drop", "enable", "disable", "toggle", "set", "post", "put", "patch",
    "backup", "dump", "export", "sync", "scan", "refresh", "reload", "build", "hook", "webhook",
    "logout", "signout", "revoke", "rotate", "migrate", "rollback", "merge", "commit", "publish",
}

SAFE_FALLBACK_WORDLIST = [
    "", "admin", "api", "docs", "redoc", "openapi.json", "swagger.json", "swagger", "health", "healthz",
    "status", "metrics", "config", "settings", "login", "console", "debug", "api/v1",
    "api/health", "api/status", "api/tags", "api/version", "api/ps", "v1/models", "api/chat",
    "api/generate", "api/config", "robots.txt", "sitemap.xml", "favicon.ico", ".env", ".git/HEAD",
    "state", "terminal", "shell", "files", "download", "logs", "log", "info",
    "version", "ping", "ui", "app", "dashboard", "static", "assets", "users", "user", "me",
    "jobs", "queue", "orders", "trades", "positions", "desks", "desk", "model", "models", "chat",
    "tmp", "test", "server-status", "actuator", "_stcore/health",
]

_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


def is_unsafe_path(path: str) -> bool:
    words = re.split(r"[^A-Za-z0-9]+", path.lower())
    return any(word in UNSAFE_WORDS for word in words if word)


def clean_wordlist(words: list) -> list[str]:
    seen: list[str] = []
    for raw in words or []:
        word = str(raw).strip().lstrip("/")
        if is_unsafe_path(word) or word in seen:
            continue
        seen.append(word)
    return seen


@dataclass
class Response:
    status: int
    length: int
    ctype: str
    title: str
    location: str
    server: str
    body: bytes = b""   # kept only for tiny banner/openapi use, never written out


@dataclass
class ServiceResult:
    host: str
    port: int
    kind: str                       # loopback | lan | vpn
    http: bool = False
    banner: str = ""
    catch_all: bool = False
    root: Response | None = None
    hits: list[tuple[str, Response]] = field(default_factory=list)
    openapi_writes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Fetcher:
    """GET-only client. The scope check runs before any socket is opened."""

    def __init__(self, scope: Scope, timeout: float = 3.0, rps: float = 15.0, keep_body: int = 262144) -> None:
        self.scope = scope
        self.timeout = timeout
        self.interval = 1.0 / max(0.5, float(rps))
        self.keep_body = keep_body
        self._last = 0.0
        self.requests = 0

    def get(self, host: str, port: int, path: str) -> Response | None:
        ip = self.scope.require(host)
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        conn = http.client.HTTPConnection(ip, int(port), timeout=self.timeout)
        try:
            conn.request("GET", "/" + path.lstrip("/"), headers={"User-Agent": "sovereign-m-selftest/1 (read-only)", "Accept": "*/*", "Connection": "close"})
            resp = conn.getresponse()
            data = resp.read(self.keep_body)
            title = ""
            match = _TITLE.search(data[:8192])
            if match:
                title = redact(match.group(1).decode("utf-8", "replace").strip())[:80]
            return Response(
                status=resp.status,
                length=int(resp.getheader("Content-Length") or len(data)),
                ctype=(resp.getheader("Content-Type") or "").split(";")[0],
                title=title,
                location=(resp.getheader("Location") or "")[:120],
                server=(resp.getheader("Server") or "")[:60],
                body=data if len(data) <= 65536 else b"",
            )
        except (OSError, http.client.HTTPException):
            return None
        finally:
            conn.close()


def probe_service(fetcher: Fetcher, host: str, port: int, kind: str, wordlist: list[str]) -> ServiceResult:
    result = ServiceResult(host, port, kind)
    root = fetcher.get(host, port, "")
    if root is None:
        result.notes.append("no HTTP answer")
        return result
    result.http = True
    result.root = root
    body = root.body[:200].decode("utf-8", "replace")
    if "ollama is running" in body.lower():
        result.banner = "Ollama"
    elif root.server:
        result.banner = root.server.split("/")[0]
    # Soft-404 baseline: if a nonsense path returns 200, every 200 is meaningless.
    junk = fetcher.get(host, port, "selftest-" + secrets.token_hex(6))
    if junk is not None and junk.status == 200:
        result.catch_all = True
        result.notes.append("answers 200 for any path (catch-all or single-page app), so the path list cannot tell endpoints apart")
    else:
        for word in wordlist:
            if word == "":
                continue
            resp = fetcher.get(host, port, word)
            if resp is None or resp.status in (404, 400, 410):
                continue
            result.hits.append((word, resp))
            if word in ("openapi.json", "swagger.json") and resp.status == 200:
                result.openapi_writes = openapi_writes(resp.body)
    return result


def openapi_writes(body: bytes) -> list[str]:
    try:
        spec = json.loads(body.decode("utf-8", "replace"))
        paths = spec.get("paths", {})
    except (ValueError, AttributeError):
        return []
    out: list[str] = []
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method in ops:
            if method.lower() in {"post", "put", "delete", "patch"}:
                out.append(f"{method.upper()} {path}")
    return sorted(out)


def analyze(results: list[ServiceResult], sensitive: dict[str, str], firewall_inactive: bool) -> list[Finding]:
    """One finding per (port, exposure): the same service on two LAN addresses is one finding."""
    groups: dict[tuple[int, str], list[ServiceResult]] = {}
    for res in results:
        if res.http and res.root is not None:
            groups.setdefault((res.port, res.kind), []).append(res)
    findings: list[Finding] = []
    for (port, exposure), members in sorted(groups.items()):
        res = members[0]
        hosts = sorted(m.host for m in members)
        where = f"port {port} ({exposure}: {', '.join(hosts)})"
        key = f"{exposure}.{port}"
        label = res.banner or "HTTP service"
        root = res.root
        protected_root = root.status in (401, 403) or (300 <= root.status < 400 and re.search(r"login|signin|auth", root.location, re.I) is not None)
        open_200 = [(w, r) for w, r in res.hits if r.status == 200]
        post_only = [w for w, r in res.hits if r.status == 405]
        write_hints: list[str] = []
        terminal = res.banner == "ttyd" or "terminal" in root.title.lower()
        if res.banner == "Ollama":
            write_hints.append("Ollama's documented API includes POST /api/pull, /api/delete, /api/create and /api/generate (from its public docs; not sent by this scan)")
        if terminal:
            write_hints.append("the page is a web terminal; if it connects without a login it hands out a shell session (the scan did not connect to it)")
        if res.openapi_writes:
            write_hints.append("its OpenAPI document lists: " + ", ".join(res.openapi_writes[:12]) + (" ..." if len(res.openapi_writes) > 12 else ""))
        if post_only:
            write_hints.append("answers 405 to GET (exists, accepts other methods): " + ", ".join("/" + w for w in post_only[:8]))
        port_note = sensitive.get(str(port))

        if protected_root:
            findings.append(Finding("web", f"web.protected.{key}", "info", f"{label} on {where} asks for a login",
                                    f"GET / answered {root.status}" + (f" -> {root.location}" if root.location else "") + ".", "", {"hosts": hosts, "port": port}))
            continue
        if root.status != 200 and not open_200:
            findings.append(Finding("web", f"web.other.{key}", "info", f"{label} on {where} answers HTTP {root.status}", "", "", {"hosts": hosts, "port": port}))
            continue

        writes = bool(res.openapi_writes) or res.banner == "Ollama" or bool(post_only) or terminal
        if exposure == "lan":
            severity = "high" if (writes or port_note) else "med"
        elif exposure == "vpn":
            severity = "med" if (writes or port_note) else "low"
        else:
            severity = "low" if writes else "info"
        endpoints = ", ".join("/" + w for w, _ in open_200[:12]) or "/"
        detail = [f"{label} on {where} answered 200 with no login. Paths that answered 200: {endpoints}."]
        if root.title:
            detail.append(f"Page title: {root.title!r}.")
        if res.catch_all:
            detail.append("It answers 200 for every path, so individual endpoints were not enumerated. It may be a single-page app with its own login screen inside the page; the scan cannot tell. UNVERIFIED.")
        if write_hints:
            detail.append("Possible state-changing endpoints (inferred, UNVERIFIED, never called): " + "; ".join(write_hints) + ".")
        if port_note:
            detail.append(f"Why it matters: {port_note}.")
        if exposure == "lan":
            detail.append("Anyone on the LAN can reach this.")
        fix = "Put a login in front of it, or bind it to the loopback or VPN address only. Rebinding means restarting the service, which needs the owner's yes."
        if res.banner == "Ollama":
            fix = "Set OLLAMA_HOST to the loopback or VPN address (service override), then restart Ollama. Both steps need the owner's yes."
        elif terminal:
            fix = "Run the terminal with a login (ttyd's credential option) and bind it to the loopback or VPN address only; or stop it when not in use. Needs the owner's yes."
        findings.append(Finding("web", f"web.open.{key}", severity, f"{label} on {where} answers without a login", " ".join(detail), fix,
                                {"hosts": hosts, "port": port, "paths_200": [w for w, _ in open_200], "write_hints": write_hints}))
        for w, r in [(w, r) for w, r in open_200 if w in (".env", ".git/HEAD")]:
            findings.append(Finding("web", f"web.leak.{key}.{w}", "high", f"/{w} is readable on {where}",
                                    "A file that normally holds secrets or source history answers 200. Contents were not stored.",
                                    "Remove it from the served directory or block that path.", {"hosts": hosts, "port": port}))
    return findings
