"""Gateway state: routes, auth, limits, audit, middleware chains, upstream calls."""

from __future__ import annotations

import http.client
import importlib
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from sovereign_m.gateway.audit import AuditLog
from sovereign_m.gateway.config import Config, Route
from sovereign_m.gateway.keys import Client, KeyStore
from sovereign_m.gateway.messages import Request, Response, coerce_response
from sovereign_m.gateway.ratelimit import RateLimiter

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "proxy-connection", "expect",
}
# Never forwarded upstream: the gateway's own credentials.
CLIENT_AUTH = {"authorization", "x-m-key", "proxy-authorization"}
NO_BODY_STATUS = {204, 304}
CHUNK = 16384
MAX_BUFFERED = 64 * 1024 * 1024
HEALTH_TIMEOUT = 2.0

Handler = Callable[[Request], Response]


def error_response(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    body = b'{"error":"' + message.replace('"', "'").encode("ascii", "replace") + b'"}'
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return Response(status, merged, body)


def load_middleware(entries: list[dict[str, Any]]) -> list[Any]:
    """Build middleware from ``module:attr`` factories, called with the entry's config dict."""
    out = []
    for entry in entries:
        module_name, _, attr = entry["use"].partition(":")
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(f"cannot load middleware {entry['use']}: {exc}") from None
        instance = factory(dict(entry.get("config") or {}))
        if not callable(getattr(instance, "handle", None)):
            raise RuntimeError(f"middleware {entry['use']} has no handle(request, call_next)")
        out.append(instance)
    return out


def build_chain(middleware: list[Any], terminal: Handler) -> Handler:
    call = terminal
    for mw in reversed(middleware):
        call = _wrap(mw, call)
    return call


def _wrap(mw: Any, nxt: Handler) -> Handler:
    def call(request: Request) -> Response:
        return coerce_response(mw.handle(request, nxt))

    return call


class SecretFile:
    """Read a one-line secret from a file, re-reading when the file changes."""

    def __init__(self, path: str) -> None:
        self.path = Path(os.path.expanduser(os.path.expandvars(path)))
        self._stamp: tuple[int, int] | None = None
        self._value = ""
        self._lock = threading.Lock()

    def value(self) -> str:
        with self._lock:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                self._value = self.path.read_text(encoding="utf-8").strip()
                self._stamp = stamp
            return self._value


class Gateway:
    def __init__(self, config: Config, keys: KeyStore, audit: AuditLog,
                 limiter: RateLimiter | None = None) -> None:
        from sovereign_m import __version__

        self.version = __version__
        self.config = config
        self.keys = keys
        self.audit = audit
        self.limiter = limiter or RateLimiter()
        self.routes = sorted((r for r in config.routes if r.enabled), key=lambda r: -len(r.prefix))
        self.by_name = {r.name: r for r in self.routes}
        self._secrets = {
            r.name: SecretFile(r.upstream_auth["key_file"]) for r in self.routes if r.upstream_auth
        }
        global_mw = load_middleware(config.middleware)
        self.chains: dict[str, Handler] = {}
        self.streaming: dict[str, bool] = {}
        for route in self.routes:
            mws = (global_mw if route.use_global_middleware else []) + load_middleware(route.middleware)
            self.streaming[route.name] = not mws
            self.chains[route.name] = build_chain(mws, self._terminal(route, stream=not mws))

    # ---- auth and routing ---------------------------------------------

    def authenticate(self, headers: dict[str, str]) -> Client | None:
        candidates = []
        auth = headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            candidates.append(token.strip())
        if headers.get("x-m-key"):
            candidates.append(headers["x-m-key"].strip())
        for candidate in candidates:
            client = self.keys.authenticate(candidate)
            if client is not None:
                return client
        return None

    def resolve(self, path: str) -> tuple[Route | None, str]:
        for route in self.routes:
            if path.startswith(route.prefix) or path == route.prefix.rstrip("/"):
                rest = path[len(route.prefix) - 1:] or "/"
                return route, rest
        return None, ""

    # ---- upstream ------------------------------------------------------

    def _connect(self, route: Route, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if route.scheme == "https" else http.client.HTTPConnection
        return cls(route.host, route.port, timeout=timeout)

    def _upstream_headers(self, route: Route, request: Request, body: bytes) -> dict[str, str]:
        listed = {t.strip().lower() for t in request.headers.get("connection", "").split(",") if t.strip()}
        out = {
            k: v for k, v in request.headers.items()
            if k not in HOP_BY_HOP and k not in CLIENT_AUTH and k not in listed
            and k not in {"host", "content-length"}
        }
        default_port = 443 if route.scheme == "https" else 80
        out["host"] = route.host if route.port == default_port else f"{route.host}:{route.port}"
        if body or request.method in {"POST", "PUT", "PATCH"}:
            out["content-length"] = str(len(body))
        if route.upstream_auth:
            cfg = route.upstream_auth
            secret = self._secrets[route.name].value()  # raises on a missing file
            out[cfg["header"].lower()] = f"{cfg['scheme']} {secret}".strip()
        return out

    def _terminal(self, route: Route, stream: bool) -> Handler:
        def call(request: Request) -> Response:
            return self.forward(route, request, stream)

        return call

    def forward(self, route: Route, request: Request, stream: bool) -> Response:
        path = request.path
        if route.strip_prefix:
            upstream_path = route.base_path + path
        else:
            upstream_path = route.base_path + route.prefix.rstrip("/") + path
        conn = self._connect(route, route.timeout)
        try:
            headers = self._upstream_headers(route, request, request.body)
            conn.request(request.method, upstream_path or "/", body=request.body or None, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError):
            conn.close()
            return error_response(504, "upstream timed out")
        except OSError as exc:
            conn.close()
            if isinstance(exc, FileNotFoundError):  # upstream key file missing
                sys.stderr.write(f"gateway: route {route.name}: upstream key file unreadable\n")
            return error_response(502, "upstream unavailable")
        except http.client.HTTPException:
            conn.close()
            return error_response(502, "bad upstream response")
        out = _response_headers(resp.getheaders())
        status = resp.status
        no_body = request.method == "HEAD" or status in NO_BODY_STATUS
        if stream:
            return Response(status, out, b"", stream=None if no_body else _chunks(resp, conn))
        try:
            body = b"" if no_body else _read_limited(resp, MAX_BUFFERED)
        except (socket.timeout, TimeoutError):
            return error_response(504, "upstream timed out")
        except (OSError, http.client.HTTPException, OverflowError):
            return error_response(502, "bad upstream response")
        finally:
            conn.close()
        return Response(status, out, body)

    # ---- health --------------------------------------------------------

    def probe(self, route: Route) -> dict[str, Any]:
        started = time.monotonic()
        conn = self._connect(route, min(route.timeout, HEALTH_TIMEOUT))
        try:
            probe = Request("GET", route.health_path, route.name, "-", {}, b"")
            headers = self._upstream_headers(route, probe, b"")
            conn.request("GET", route.base_path + route.health_path, headers=headers)
            resp = conn.getresponse()
            resp.read(1024)
            up = resp.status < 500
            code: int | None = resp.status
        except (OSError, http.client.HTTPException):
            up, code = False, None
        finally:
            conn.close()
        return {"up": up, "status": code, "latency_ms": round((time.monotonic() - started) * 1000, 1)}

    def probe_all(self, routes: list[Route]) -> dict[str, dict[str, Any]]:
        if not routes:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(routes))) as pool:
            results = list(pool.map(self.probe, routes))
        return {r.name: res for r, res in zip(routes, results)}


def _response_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        if key in HOP_BY_HOP or key in {"date", "server"}:
            continue
        if key in out:
            out[key] += ("\n" if key == "set-cookie" else ", ") + value
        else:
            out[key] = value
    return out


def _chunks(resp: http.client.HTTPResponse, conn: http.client.HTTPConnection) -> Iterator[bytes]:
    try:
        while True:
            data = resp.read1(CHUNK)
            if not data:
                return
            yield data
    finally:
        conn.close()


def _read_limited(resp: http.client.HTTPResponse, limit: int) -> bytes:
    parts: list[bytes] = []
    total = 0
    while True:
        data = resp.read(CHUNK)
        if not data:
            return b"".join(parts)
        total += len(data)
        if total > limit:
            raise OverflowError("response too large to buffer")
        parts.append(data)
