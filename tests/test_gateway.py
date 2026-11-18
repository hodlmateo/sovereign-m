import http.client
import json
import os
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sovereign_m.cli import main as root_main
from sovereign_m.gateway import policy
from sovereign_m.gateway.audit import AuditLog
from sovereign_m.gateway.cli import main
from sovereign_m.gateway.config import ConfigError, load_config, parse_config
from sovereign_m.gateway.core import Gateway
from sovereign_m.gateway.keys import KeyFileError, KeyStore
from sovereign_m.gateway.ratelimit import RateLimiter
from sovereign_m.gateway.server import BindRefused, make_server, resolve_bind

LOOP = ".".join(("127", "0", "0", "1"))
ANY = ".".join(("0", "0", "0", "0"))
OTHER = ".".join(("192", "0", "2", "10"))
SECRET_BODY = "body-secret-marker-7731"
UPSTREAM_KEY = "upstream-backend-key-5521"


# ---- fixtures ------------------------------------------------------------


class Upstream:
    """A fake backend that records what it received."""

    def __init__(self):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.seen.append({"method": self.command, "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if self.path.startswith("/stream"):
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Keep-Alive", "timeout=5")
                    self.end_headers()
                    for part in (b"one", b"two", b"three"):
                        self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                payload = json.dumps({"path": self.path, "echo": len(body)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Upstream", "yes")
                self.send_header("Keep-Alive", "timeout=5")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = ThreadingHTTPServer((LOOP, 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    up = Upstream()
    yield up
    up.close()


class Rig:
    def __init__(self, tmp_path, upstream, routes=None, middleware=None, **extra):
        self.tmp = tmp_path
        self.upstream = upstream
        raw = {
            "server": {"host": "loopback", "port": 0},
            "auth": {"keys_file": str(tmp_path / "keys.json"), "default_rate_per_min": 0},
            "audit": {"path": str(tmp_path / "audit.jsonl"), "max_bytes": 10_000_000},
            "middleware": middleware or [],
            "routes": routes if routes is not None else [self.route("svc")],
        }
        for key, value in extra.items():
            raw[key].update(value)
        self.cfg = parse_config(raw, {})
        self.store = KeyStore(tmp_path / "keys.json")
        self.audit = AuditLog(tmp_path / "audit.jsonl", 10_000_000, 3, "strip", True)
        self.limiter = RateLimiter()
        self.gateway = Gateway(self.cfg, self.store, self.audit, self.limiter)
        self.server, _ = make_server(self.cfg, self.gateway)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def route(self, name, **kw):
        base = {
            "name": name,
            "prefix": f"/{name}/",
            "upstream": f"http://localhost:{self.upstream.port}",
            "methods": ["GET", "POST"],
            "allow": ["/*"],
        }
        base.update(kw)
        return base

    def key(self, client="alice", scopes=None, rate=None):
        return self.store.add(client, scopes, rate)

    def request(self, method, path, key=None, headers=None, body=None, header="authorization"):
        conn = http.client.HTTPConnection(LOOP, self.port, timeout=10)
        hdrs = dict(headers or {})
        if key:
            hdrs["Authorization" if header == "authorization" else "X-M-Key"] = (
                f"Bearer {key}" if header == "authorization" else key
            )
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def audit_lines(self, expected=0, timeout=2.0):
        # The server writes the audit line after the response is sent, so poll
        # (bounded) until the expected number of lines has landed.
        path = self.tmp / "audit.jsonl"
        deadline = time.monotonic() + timeout
        while True:
            lines = path.read_text().splitlines() if path.exists() else []
            if len(lines) >= expected or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self.audit.close()
        return lines

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.audit.close()


@pytest.fixture
def rig(tmp_path, upstream):
    made = []

    def build(**kw):
        r = Rig(tmp_path, upstream, **kw)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


# ---- proxying ------------------------------------------------------------


def test_proxies_and_strips_prefix(rig):
    r = rig()
    key = r.key()
    status, headers, body = r.request("GET", "/svc/hello?x=1", key)
    assert status == 200
    assert json.loads(body)["path"] == "/hello?x=1"
    assert headers["x-upstream"] == "yes"
    assert r.upstream.seen[-1]["method"] == "GET"


def test_keep_prefix_when_strip_prefix_false(rig, upstream):
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/*"], "strip_prefix": False}])
    key = r.key()
    status, _, body = r.request("GET", "/svc/a", key)
    assert status == 200 and json.loads(body)["path"] == "/svc/a"


def test_post_body_reaches_upstream_and_streams_back(rig):
    r = rig()
    key = r.key()
    status, _, body = r.request("POST", "/svc/in", key, body=b"x" * 500)
    assert status == 200 and json.loads(body)["echo"] == 500
    status, headers, body = r.request("GET", "/svc/stream", key)
    assert status == 200 and body == b"onetwothree"
    assert headers.get("transfer-encoding") == "chunked"


def test_x_m_key_header_works(rig):
    r = rig()
    key = r.key()
    assert r.request("GET", "/svc/a", key, header="x-m-key")[0] == 200


def test_gateway_key_never_forwarded_upstream(rig):
    r = rig()
    key = r.key()
    r.request("GET", "/svc/a", key, headers={"X-M-Key": key})
    r.request("GET", "/svc/a", None, headers={"Authorization": f"Bearer {key}", "X-M-Key": key})
    for seen in r.upstream.seen:
        assert "authorization" not in seen["headers"]
        assert "x-m-key" not in seen["headers"]
        assert key not in json.dumps(seen["headers"])


def test_upstream_auth_injected_from_key_file_and_not_logged(rig, upstream, tmp_path, capsys):
    key_file = tmp_path / "up.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, (UPSTREAM_KEY + "\n").encode())
    os.close(fd)
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/*"],
                     "upstream_auth": {"header": "Authorization", "scheme": "Bearer",
                                       "key_file": str(key_file)}}])
    key = r.key()
    status, headers, body = r.request("GET", "/svc/a", key)
    assert status == 200
    assert upstream.seen[-1]["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert UPSTREAM_KEY not in body.decode() and UPSTREAM_KEY not in json.dumps(headers)
    assert UPSTREAM_KEY not in "\n".join(r.audit_lines(1))
    captured = capsys.readouterr()
    assert UPSTREAM_KEY not in captured.out + captured.err


def test_hop_by_hop_headers_dropped(rig):
    r = rig()
    key = r.key()
    status, headers, _ = r.request("GET", "/svc/stream", key, headers={"Keep-Alive": "9", "TE": "trailers"})
    assert status == 200
    assert "keep-alive" not in headers
    assert "keep-alive" not in r.upstream.seen[-1]["headers"]
    assert "te" not in r.upstream.seen[-1]["headers"]
    status, headers, _ = r.request("GET", "/svc/plain", key)
    assert "keep-alive" not in headers


def test_oversize_body_is_413_and_never_reaches_upstream(rig, upstream):
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["POST"], "allow": ["/*"], "max_body_bytes": 100}])
    key = r.key()
    before = len(upstream.seen)
    status, _, _ = r.request("POST", "/svc/in", key, body=b"x" * 101)
    assert status == 413
    assert len(upstream.seen) == before
    assert r.request("POST", "/svc/in", key, body=b"x" * 100)[0] == 200


def test_upstream_down_is_502(rig, tmp_path):
    dead = socket.socket()
    dead.bind((LOOP, 0))
    port = dead.getsockname()[1]
    dead.close()
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{port}",
                     "methods": ["GET"], "allow": ["/*"], "timeout": 2}])
    key = r.key()
    assert r.request("GET", "/svc/a", key)[0] == 502


# ---- allowlist and denylist ---------------------------------------------


def ollama_route(port, **kw):
    base = {
        "name": "ollama", "prefix": "/ollama/", "upstream": f"http://localhost:{port}",
        "methods": ["GET", "POST", "DELETE"],
        "allow": ["GET /api/tags", "GET /api/version", "GET /api/ps", "POST /api/chat", "POST /api/generate",
                  "POST /api/embed", "POST /v1/chat/completions", "GET /v1/models"],
    }
    base.update(kw)
    return base


def test_ollama_allow_list_and_default_admin_block(rig, upstream):
    r = rig(routes=[ollama_route(upstream.port)])
    key = r.key()
    assert r.request("GET", "/ollama/api/tags", key)[0] == 200
    assert r.request("POST", "/ollama/api/chat", key, body=b"{}")[0] == 200
    assert r.request("POST", "/ollama/api/tags", key, body=b"{}")[0] == 403  # method not allowed for that path
    for path in ("/api/pull", "/api/delete", "/api/create", "/api/copy", "/api/push", "/api/blobs/sha256:abc"):
        assert r.request("POST", "/ollama" + path, key, body=b"{}")[0] == 403, path
    seen_paths = [s["path"] for s in r.upstream.seen]
    assert not any("pull" in p or "delete" in p or "blobs" in p for p in seen_paths)


@pytest.mark.parametrize("variant", ["/api/pull/", "/API/PULL", "/api/%70ull", "/api/./pull", "/api/x/../pull",
                                     "//api/pull", "/api//pull", "/api%2fpull", "/api\\pull"])
def test_admin_block_survives_path_tricks(rig, upstream, variant):
    r = rig(routes=[ollama_route(upstream.port, allow=["/*"])])
    key = r.key()
    status = r.request("POST", "/ollama" + variant, key, body=b"{}")[0]
    assert status in (400, 403)
    assert not any("pull" in s["path"].lower() for s in r.upstream.seen)


def test_admin_allowed_only_with_allow_admin(rig, upstream):
    r = rig(routes=[ollama_route(upstream.port, allow=["/*"], allow_admin=True)])
    key = r.key()
    assert r.request("POST", "/ollama/api/pull", key, body=b"{}")[0] == 200
    assert r.request("POST", "/ollama/api/blobs/sha256:abc", key, body=b"{}")[0] == 200


def test_docs_and_debug_blocked_by_default_allowed_with_allow_docs(rig, upstream):
    route = {"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
             "methods": ["GET"], "allow": ["/*"]}
    r = rig(routes=[route])
    key = r.key()
    for path in ("/docs", "/redoc", "/openapi.json", "/debug", "/debug/vars"):
        assert r.request("GET", "/svc" + path, key)[0] == 403, path
    assert r.request("GET", "/svc/ok", key)[0] == 200
    r.close()
    r = rig(routes=[dict(route, allow_docs=True)])
    assert r.request("GET", "/svc/docs", r.key("bob"))[0] == 200


def test_route_denylist_and_empty_allowlist(rig, upstream):
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/api/*", "/static/"], "deny": ["/api/admin/*"]},
                    {"name": "none", "prefix": "/none/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"]}])
    key = r.key()
    assert r.request("GET", "/svc/api/x", key)[0] == 200
    assert r.request("GET", "/svc/static/a.css", key)[0] == 200
    assert r.request("GET", "/svc/api/admin/x", key)[0] == 403
    assert r.request("GET", "/svc/other", key)[0] == 403
    assert r.request("GET", "/none/x", key)[0] == 403
    assert r.request("GET", "/nothing/x", key)[0] == 404
    assert r.request("DELETE", "/svc/api/x", key)[0] == 405


def test_policy_unit_matching():
    assert policy.normalize("/a/b?x=1")[0] == "/a/b"
    for bad in ("/a/../b", "/a/%2e%2e/b", "/a//b", "x", "/a%00", "/a%2Fb"):
        assert policy.normalize(bad)[0] == ""
    assert policy.match("/api/", "/api/x") and policy.match("/api/", "/api")
    assert policy.match("/api/*", "/api/x/y") and not policy.match("/api/*", "/other")


# ---- auth, scopes, rate limits -------------------------------------------


def test_401_403_and_scope(rig, upstream):
    r = rig(routes=[{"name": "a", "prefix": "/a/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/*"]},
                    {"name": "b", "prefix": "/b/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/*"]}])
    key = r.key("alice", ["a"])
    assert r.request("GET", "/a/x")[0] == 401
    assert r.request("GET", "/a/x", "not-a-real-key")[0] == 401
    status, headers, _ = r.request("GET", "/nothing")
    assert status == 401 and headers["www-authenticate"] == "Bearer"  # no route probing without a key
    assert r.request("GET", "/a/x", key)[0] == 200
    assert r.request("GET", "/b/x", key)[0] == 403
    r.store.revoke("alice")
    assert r.request("GET", "/a/x", key)[0] == 401


def test_rate_limit_429_with_retry_after(rig):
    r = rig()
    key = r.key("slow", rate=2)
    assert r.request("GET", "/svc/a", key)[0] == 200
    assert r.request("GET", "/svc/a", key)[0] == 200
    status, headers, _ = r.request("GET", "/svc/a", key)
    assert status == 429
    assert int(headers["retry-after"]) >= 1
    other = r.key("fast", rate=100)
    assert r.request("GET", "/svc/a", other)[0] == 200  # buckets are per client


def test_route_rate_limit(rig, upstream):
    r = rig(routes=[{"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
                     "methods": ["GET"], "allow": ["/*"], "rate_per_min": 1}])
    key = r.key()
    assert r.request("GET", "/svc/a", key)[0] == 200
    assert r.request("GET", "/svc/a", key)[0] == 429


def test_token_bucket_refills_with_clock():
    now = [0.0]
    limiter = RateLimiter(lambda: now[0])
    assert limiter.check("c", "*", 60) == 0
    for _ in range(59):
        assert limiter.check("c", "*", 60) == 0
    assert limiter.check("c", "*", 60) == 1
    now[0] += 2.0
    assert limiter.check("c", "*", 60) == 0
    assert limiter.check("c", "*", 0) == 0  # zero means unlimited


# ---- key storage ---------------------------------------------------------


def test_key_file_mode_and_no_plaintext(tmp_path):
    store = KeyStore(tmp_path / "k" / "keys.json")
    key = store.add("alice", ["a"], 10)
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    text = store.path.read_text()
    assert key not in text
    data = json.loads(text)["clients"]["alice"]
    assert set(data) >= {"salt", "sha256"} and len(data["sha256"]) == 64
    other = KeyStore(store.path)
    assert other.authenticate(key).client_id == "alice"
    assert other.authenticate(key + "x") is None
    assert [row["client"] for row in store.list()] == ["alice"]
    assert key not in json.dumps(store.list())


def test_salts_differ_per_key(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.add("a")
    store.add("b")
    data = json.loads(store.path.read_text())["clients"]
    assert data["a"]["salt"] != data["b"]["salt"]


def test_key_file_refused_when_world_readable(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    key = store.add("alice")
    os.chmod(store.path, 0o644)
    with pytest.raises(KeyFileError):
        KeyStore(store.path).load()
    # a running gateway fails closed instead of crashing
    live = KeyStore(store.path)
    assert live.authenticate(key) is None
    assert "readable" in live.last_error
    os.chmod(store.path, 0o600)
    assert live.authenticate(key).client_id == "alice"


def test_duplicate_and_bad_client_names(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.add("alice")
    with pytest.raises(KeyFileError):
        store.add("alice")
    with pytest.raises(KeyFileError):
        store.add("../evil")
    assert store.revoke("alice") and not store.revoke("alice")


def test_cli_keys_add_prints_key_once_and_list_has_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOVEREIGN_M_WORKSPACE", str(tmp_path))
    assert main(["keys", "add", "alice", "--scopes", "m-chat", "--rate", "30/min"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out) >= 40 and " " not in out
    assert main(["keys", "list"]) == 0
    listed = capsys.readouterr().out
    assert "alice" in listed and "m-chat" in listed and "30/min" in listed and out not in listed
    assert main(["keys", "revoke", "alice"]) == 0
    assert main(["keys", "revoke", "alice"]) == 1
    assert main(["keys", "add", "bad name"]) == 2
    path = tmp_path / "gateway" / "keys.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    capsys.readouterr()


# ---- audit ---------------------------------------------------------------


def test_audit_has_no_bodies_keys_or_query(rig):
    r = rig()
    key = r.key()
    r.request("POST", "/svc/in?token=qsecret-9911", key, body=SECRET_BODY.encode())
    r.request("GET", "/svc/a", "wrong-key-value")
    r.request("GET", "/svc/a", key, headers={"X-Cache": "x"})
    lines = r.audit_lines(3)
    text = "\n".join(lines)
    assert SECRET_BODY not in text and key not in text and "qsecret" not in text and "wrong-key-value" not in text
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 3
    first = rows[0]
    assert set(first) >= {"ts", "client_id", "route", "method", "path", "status", "bytes_in", "bytes_out",
                          "duration_ms"}
    assert first["client_id"] == "alice" and first["route"] == "svc" and first["path"] == "/svc/in"
    assert first["bytes_in"] == len(SECRET_BODY) and first["bytes_out"] > 0
    assert rows[1]["status"] == 401 and rows[1]["client_id"] == "-"
    assert stat.S_IMODE(os.stat(r.tmp / "audit.jsonl").st_mode) == 0o600


def test_audit_redact_mode_and_rotation(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", max_bytes=400, keep=2, query="redact")
    for i in range(20):
        log.log(client_id="c", route="r", method="GET", target=f"/p?secret=abc{i}&k=v", status=200,
                bytes_in=0, bytes_out=1, duration_ms=1.0)
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["a.jsonl", "a.jsonl.1", "a.jsonl.2"]
    everything = "".join((tmp_path / n).read_text() for n in names)
    assert "abc" not in everything and "secret=***" in everything
    for n in names:
        assert stat.S_IMODE(os.stat(tmp_path / n).st_mode) == 0o600


def test_cache_header_recorded(rig):
    r = rig(middleware=[{"use": f"{__name__}:make_tag", "config": {}}])
    key = r.key()
    status, headers, _ = r.request("GET", "/svc/a", key)
    assert headers["x-cache"] == "HIT"
    assert json.loads(r.audit_lines(1)[0])["cache"] == "HIT"


# ---- health --------------------------------------------------------------


def test_public_health_reveals_only_ok_and_version(rig):
    r = rig()
    status, _, body = r.request("GET", "/_gateway/health")
    data = json.loads(body)
    assert status == 200 and set(data) == {"ok", "version"} and data["ok"] is True
    assert "svc" not in body.decode()


def test_routes_health_needs_auth_and_reports_up_down(rig, upstream):
    dead = socket.socket()
    dead.bind((LOOP, 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    r = rig(routes=[{"name": "up", "prefix": "/up/", "upstream": f"http://localhost:{upstream.port}",
                     "allow": ["/*"], "health_path": "/ping"},
                    {"name": "down", "prefix": "/down/", "upstream": f"http://localhost:{dead_port}",
                     "allow": ["/*"]},
                    {"name": "hidden", "prefix": "/hidden/", "upstream": f"http://localhost:{upstream.port}",
                     "allow": ["/*"]}])
    assert r.request("GET", "/_gateway/routes/health")[0] == 401
    key = r.key("alice", ["up", "down"])
    status, _, body = r.request("GET", "/_gateway/routes/health", key)
    data = json.loads(body)["routes"]
    assert status == 200 and set(data) == {"up", "down"}
    assert data["up"]["up"] is True and data["up"]["latency_ms"] >= 0
    assert data["down"]["up"] is False
    assert upstream.seen[-1]["path"] == "/ping"
    assert str(upstream.port) not in body.decode()


# ---- middleware ----------------------------------------------------------

ORDER = []


class Trace:
    def __init__(self, cfg):
        self.name = cfg["name"]

    def handle(self, request, call_next):
        ORDER.append(f"in:{self.name}")
        request.headers["x-seen-" + self.name] = "1"
        response = call_next(request)
        ORDER.append(f"out:{self.name}")
        response.headers["x-trace-" + self.name] = "1"
        return response


class ShortCircuit:
    def __init__(self, cfg):
        pass

    def handle(self, request, call_next):
        from sovereign_m.gateway.messages import Response

        ORDER.append("short")
        return Response(203, {"x-cache": "HIT"}, b"from-middleware")


def make_trace(cfg):
    return Trace({"name": cfg.get("name", "t")})


def make_short(cfg):
    return ShortCircuit(cfg)


def make_tag(cfg):
    class Tag:
        def handle(self, request, call_next):
            resp = call_next(request)
            resp.headers["x-cache"] = "HIT"
            return resp

    return Tag()


def test_middleware_chain_order_global_then_route(rig, upstream):
    ORDER.clear()
    mod = __name__
    route = {"name": "svc", "prefix": "/svc/", "upstream": f"http://localhost:{upstream.port}",
             "methods": ["GET"], "allow": ["/*"],
             "middleware": [{"use": f"{mod}:make_trace", "config": {"name": "route"}}]}
    r = rig(routes=[route], middleware=[{"use": f"{mod}:make_trace", "config": {"name": "global"}}])
    key = r.key()
    status, headers, _ = r.request("GET", "/svc/a", key)
    assert status == 200
    assert ORDER == ["in:global", "in:route", "out:route", "out:global"]
    assert headers["x-trace-global"] == "1" and headers["x-trace-route"] == "1"
    seen = upstream.seen[-1]["headers"]
    assert seen["x-seen-global"] == "1" and "authorization" not in seen


def test_middleware_receives_contract_request(rig):
    captured = {}

    class Spy:
        def handle(self, request, call_next):
            captured.update(method=request.method, path=request.path, route=request.route,
                            client=request.client_id, headers=dict(request.headers), body=request.body)
            return call_next(request)

    import types
    mod = types.ModuleType("gw_spy_mod")
    mod.make = lambda cfg: Spy()
    sys.modules["gw_spy_mod"] = mod
    try:
        r = rig(middleware=[{"use": "gw_spy_mod:make", "config": {}}])
        key = r.key("alice")
        r.request("POST", "/svc/p?q=1", key, body=b"abc", headers={"X-Thing": "v"})
    finally:
        del sys.modules["gw_spy_mod"]
    assert captured["method"] == "POST" and captured["path"] == "/p?q=1" and captured["route"] == "svc"
    assert captured["client"] == "alice" and captured["body"] == b"abc"
    assert captured["headers"]["x-thing"] == "v"
    assert "authorization" not in captured["headers"] and key not in json.dumps(captured["headers"])


def test_short_circuit_middleware_skips_upstream(rig, upstream):
    ORDER.clear()
    r = rig(middleware=[{"use": f"{__name__}:make_short", "config": {}}])
    key = r.key()
    before = len(upstream.seen)
    status, headers, body = r.request("GET", "/svc/a", key)
    assert (status, body, headers["x-cache"]) == (203, b"from-middleware", "HIT")
    assert len(upstream.seen) == before and ORDER == ["short"]


def test_bad_middleware_path_fails_at_startup(tmp_path, upstream):
    with pytest.raises(RuntimeError):
        rig_ = Rig(tmp_path, upstream, middleware=[{"use": "no_such_module_xyz:make", "config": {}}])
        rig_.close()


# ---- bind safety ---------------------------------------------------------


def test_default_bind_is_loopback_and_wildcard_refused():
    assert resolve_bind("loopback", False) == (LOOP, [])
    assert resolve_bind("localhost", False)[0] == LOOP
    for host in (ANY, "::", OTHER, "example.com"):
        with pytest.raises(BindRefused):
            resolve_bind(host, False)
    address, warnings = resolve_bind(ANY, True)
    assert address == ANY and "WARNING" in warnings[0]
    assert "WARNING" in resolve_bind(OTHER, True)[1][0]


def test_make_server_refuses_wildcard_without_flag(tmp_path, upstream):
    r = Rig(tmp_path, upstream)
    try:
        with pytest.raises(BindRefused):
            make_server(r.cfg, r.gateway, host=ANY, port=0)
        server, warnings = make_server(r.cfg, r.gateway, host="loopback", port=0)
        assert warnings == [] and server.server_address[0] == LOOP
        server.server_close()
    finally:
        r.close()


def test_cli_serve_refuses_wildcard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOVEREIGN_M_WORKSPACE", str(tmp_path))
    assert main(["serve", "--host", ANY, "--port", "0"]) == 2
    assert "refusing" in capsys.readouterr().err


# ---- config and CLI ------------------------------------------------------


def test_example_config_parses_and_unset_ports_disable_routes():
    cfg = load_config(None, env={})
    names = [r.name for r in cfg.routes]
    assert names == ["m-chat", "trading-ui", "institute-portal", "ollama", "llama-server", "scheduler-status"]
    assert not any(r.enabled for r in cfg.routes)
    assert cfg.port == 8787 and cfg.host == "loopback" and cfg.allow_nonloopback is False
    env = {"OLLAMA_PORT": "9"}
    ollama = [r for r in load_config(None, env=env).routes if r.name == "ollama"][0]
    assert ollama.enabled and ollama.port == 9 and ollama.host == "localhost"
    assert ("GET", "/api/tags") in ollama.allow and ("POST", "/api/chat") in ollama.allow
    assert ollama.allow_admin is False


def test_config_errors():
    for bad in ({"routes": [{"name": "a b", "prefix": "/x/"}]},
                {"routes": [{"name": "a", "prefix": "x"}]},
                {"routes": [{"name": "a", "prefix": "/_gateway/", "upstream": "http://h:1"}]},
                {"routes": [{"name": "a", "prefix": "/a/", "upstream": "ftp://h"}]},
                {"routes": [{"name": "a", "prefix": "/a/", "upstream": "http://h:1", "websocket": True}]},
                {"routes": [{"name": "a", "prefix": "/a/", "upstream": "http://h:1"},
                            {"name": "a", "prefix": "/b/", "upstream": "http://h:1"}]},
                {"server": {"port": "nope"}}):
        with pytest.raises(ConfigError):
            parse_config(bad, {})


def test_check_config_flags_non_loopback_upstream(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOVEREIGN_M_WORKSPACE", str(tmp_path))
    cfg = tmp_path / "gw.yaml"
    other = ".".join(("192", "0", "2", "55"))
    cfg.write_text(
        "routes:\n"
        "  - name: near\n    prefix: /near/\n    upstream: http://localhost:9\n    allow:\n      - /*\n"
        f"  - name: far\n    prefix: /far/\n    upstream: http://{other}:9\n    allow:\n      - /*\n"
    )
    assert main(["check-config", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "route near" in out and "route far" in out
    far = out.split("route far")[1]
    assert "not loopback" in far
    assert "not loopback" not in out.split("route far")[0]
    assert main(["check-config", "--config", str(cfg), "--strict"]) == 1


def test_root_cli_registers_gateway(capsys):
    assert root_main(["gateway", "help"]) == 0
    assert "serve" in capsys.readouterr().out
    assert root_main(["--help"]) == 0
    assert "gateway" in capsys.readouterr().out


def test_workspace_files_use_workspace_dir(tmp_path, monkeypatch):
    from sovereign_m.gateway.cli import audit_from, keys_path

    monkeypatch.setenv("SOVEREIGN_M_WORKSPACE", str(tmp_path))
    cfg = parse_config({}, {})
    assert keys_path(cfg) == tmp_path / "gateway" / "keys.json"
    assert audit_from(cfg).path == tmp_path / "gateway" / "audit.jsonl"
