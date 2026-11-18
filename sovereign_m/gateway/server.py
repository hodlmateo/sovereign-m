"""The HTTP front door: ThreadingHTTPServer plus a request handler."""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sovereign_m.gateway import policy
from sovereign_m.gateway.config import LOOPBACK, Config
from sovereign_m.gateway.core import HOP_BY_HOP, NO_BODY_STATUS, Gateway, error_response
from sovereign_m.gateway.messages import Request, Response


class BindRefused(ValueError):
    pass


def resolve_bind(host: str, allow_nonloopback: bool) -> tuple[str, list[str]]:
    """Return (address to bind, warnings). Refuses anything but loopback by default."""
    text = (host or "").strip().lower()
    if text in {"", "loopback", "localhost"}:
        return LOOPBACK, []
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise BindRefused(
            "host must be loopback, localhost, or an address literal (with allow_nonloopback)"
        ) from None
    if ip.is_loopback:
        return text, []
    if not allow_nonloopback:
        raise BindRefused(
            "refusing to bind a non-loopback address; "
            "set server.allow_nonloopback: true (or --allow-nonloopback) if you mean it"
        )
    if ip.is_unspecified:
        warning = ("WARNING: wildcard bind: the gateway is reachable from every network interface. "
                   "Use a firewall, and prefer one specific address.")
    else:
        warning = ("WARNING: non-loopback bind: anything that can reach this address can reach the gateway. "
                   "Keys are sent in clear text over plain HTTP; use a trusted private network.")
    return text, [warning]


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], gateway: Gateway) -> None:
        self.gateway = gateway
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hangs up is normal. Anything else is logged by type only.
        exc = sys.exc_info()[1]
        if not isinstance(exc, (ConnectionError, TimeoutError)):
            sys.stderr.write(f"gateway: connection error: {type(exc).__name__}\n")


def make_server(config: Config, gateway: Gateway, host: str | None = None, port: int | None = None,
                allow_nonloopback: bool | None = None) -> tuple[GatewayServer, list[str]]:
    allow = config.allow_nonloopback if allow_nonloopback is None else allow_nonloopback
    address, warnings = resolve_bind(config.host if host is None else host, allow)
    server = GatewayServer((address, config.port if port is None else port), gateway)
    return server, warnings


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sovereign-m-gateway"
    sys_version = ""
    server: GatewayServer

    def log_message(self, *args: Any) -> None:  # the audit log is the access log
        pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = (
        lambda self: self._serve()
    )

    # ---- one request ---------------------------------------------------

    def _serve(self) -> None:
        started = time.monotonic()
        gw = self.server.gateway
        self._ev: dict[str, Any] = {"client": "-", "route": "-", "in": 0, "out": 0, "cache": None}
        self._body_read = False
        status = 500
        try:
            status = self._dispatch(gw)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            status = 499
        except Exception as exc:  # never leak details to the client
            sys.stderr.write(f"gateway: internal error: {type(exc).__name__}\n")
            self.close_connection = True
            try:
                status = self._send(error_response(500, "internal error"))
            except OSError:
                pass
        finally:
            if not self._body_read and (
                self.headers.get("content-length", "0").strip() not in {"", "0"}
                or "transfer-encoding" in self.headers
            ):
                self.close_connection = True
            gw.audit.log(
                client_id=self._ev["client"], route=self._ev["route"], method=self.command or "-",
                target=self.path, status=status, bytes_in=self._ev["in"], bytes_out=self._ev["out"],
                duration_ms=(time.monotonic() - started) * 1000, cache=self._ev["cache"],
            )

    def _dispatch(self, gw: Gateway) -> int:
        method = self.command
        target = self.path
        bare = target.partition("?")[0]
        if bare == "/_gateway/health":
            self._ev["route"] = "_gateway"
            if method not in {"GET", "HEAD"}:
                return self._send(error_response(405, "method not allowed", {"allow": "GET, HEAD"}))
            return self._send_json({"ok": True, "version": gw.version})
        headers = self._header_dict()
        client = gw.authenticate(headers)
        if client is None:
            return self._send(error_response(401, "unauthorized", {"www-authenticate": "Bearer"}))
        self._ev["client"] = client.client_id
        if bare == "/_gateway/routes/health":
            self._ev["route"] = "_gateway"
            if method not in {"GET", "HEAD"}:
                return self._send(error_response(405, "method not allowed", {"allow": "GET, HEAD"}))
            routes = [r for r in gw.routes if client.allows(r.name)]
            return self._send_json({"routes": gw.probe_all(routes)})
        if bare.startswith("/_gateway/"):
            return self._send(error_response(404, "not found"))
        decoded, raw, query = policy.normalize(target)
        if not decoded:
            return self._send(error_response(400, "bad path"))
        route, rest = gw.resolve(decoded)
        if route is None:
            return self._send(error_response(404, "not found"))
        self._ev["route"] = route.name
        if not client.allows(route.name):
            return self._send(error_response(403, "forbidden"))
        if method not in route.methods and not (method == "HEAD" and "GET" in route.methods):
            return self._send(error_response(405, "method not allowed", {"allow": ", ".join(route.methods)}))
        if policy.refusal(route, method, rest, decoded):
            return self._send(error_response(403, "forbidden"))
        if "upgrade" in headers:
            return self._send(error_response(400, "upgrade not supported"))
        wait = gw.limiter.check(client.client_id, "*", client.rate_per_min
                                if client.rate_per_min is not None else gw.config.default_rate_per_min)
        if not wait and route.rate_per_min:
            wait = gw.limiter.check(client.client_id, route.name, route.rate_per_min)
        if wait:
            return self._send(error_response(429, "rate limit exceeded", {"retry-after": str(wait)}))
        body = self._read_body(route.max_body_bytes)
        if isinstance(body, Response):
            return self._send(body)
        raw_rest = raw[len(route.prefix) - 1:] or "/"
        request = Request(method, raw_rest + ("?" + query if query else ""), route.name,
                          client.client_id, _without_auth(headers), body)
        try:
            response = gw.chains[route.name](request)
        except Exception as exc:
            sys.stderr.write(f"gateway: route {route.name}: handler failed: {type(exc).__name__}\n")
            response = error_response(500, "internal error")
        return self._send(response)

    # ---- reading -------------------------------------------------------

    def _header_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in self.headers.items():
            key = name.lower()
            out[key] = f"{out[key]}, {value}" if key in out else value
        return out

    def _read_body(self, limit: int) -> bytes | Response:
        if "transfer-encoding" in self.headers:
            self.close_connection = True
            return error_response(411, "content-length required")
        raw = self.headers.get("content-length", "0").strip() or "0"
        if not raw.isdigit():
            self.close_connection = True
            return error_response(400, "bad content-length")
        length = int(raw)
        if length > limit:
            self.close_connection = True
            return error_response(413, "request body too large")
        data = self.rfile.read(length) if length else b""
        self._body_read = True
        if len(data) != length:
            self.close_connection = True
            return error_response(400, "short body")
        self._ev["in"] = length
        return data

    # ---- writing -------------------------------------------------------

    def _send_json(self, payload: dict) -> int:
        body = json.dumps(payload, separators=(",", ":")).encode()
        return self._send(Response(200, {"content-type": "application/json"}, body))

    def _send(self, resp: Response) -> int:
        self._ev["cache"] = resp.headers.get("x-cache")
        head = self.command == "HEAD"
        no_body = head or resp.status in NO_BODY_STATUS or 100 <= resp.status < 200
        headers = {k.lower(): v for k, v in resp.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in {"date", "server"}}
        stream = resp.stream
        chunked = False
        if stream is None:
            headers.pop("content-length", None)
            if not no_body or head:
                headers["content-length"] = str(len(resp.body))
        else:
            length = headers.get("content-length", "")
            if not length.isdigit():
                headers.pop("content-length", None)
                if self.request_version == "HTTP/1.1":
                    chunked = True
                    headers["transfer-encoding"] = "chunked"
                else:
                    self.close_connection = True
        self.send_response(resp.status)
        for name, value in headers.items():
            for part in (value.split("\n") if name == "set-cookie" else [value.replace("\n", " ")]):
                self.send_header(name, part.replace("\r", ""))
        self.end_headers()
        if no_body:
            if stream is not None:
                stream.close()  # type: ignore[attr-defined]
            return resp.status
        if stream is None:
            self.wfile.write(resp.body)
            self._ev["out"] = len(resp.body)
            return resp.status
        try:
            for chunk in stream:
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
                self._ev["out"] += len(chunk)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            # Upstream died mid-stream: cut the connection so the client sees a short read.
            self.close_connection = True
        finally:
            stream.close()  # type: ignore[attr-defined]
        return resp.status


def _without_auth(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k not in {"authorization", "x-m-key"}}
