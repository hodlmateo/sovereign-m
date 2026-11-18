"""Run the gateway against a throwaway backend. Nothing leaves this machine.

    python examples/gateway/gateway_local_demo.py

It starts a tiny backend on a free loopback port, puts a gateway in front of
it (also on a free port, with a temporary workspace), creates a key, and shows
what the gateway lets through: an allowed path, a path off the allowlist, a
blocked admin path, a missing key, and a wrong scope. The key is made in
memory and never printed.
"""

from __future__ import annotations

import http.client
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sovereign_m.gateway.audit import AuditLog
from sovereign_m.gateway.config import parse_config
from sovereign_m.gateway.core import Gateway
from sovereign_m.gateway.keys import KeyStore
from sovereign_m.gateway.server import make_server

LOOPBACK = ".".join(("127", "0", "0", "1"))


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'{"models": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


def call(port: int, method: str, path: str, key: str | None = None) -> int:
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=5)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


def main() -> int:
    backend = ThreadingHTTPServer((LOOPBACK, 0), Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        cfg = parse_config({
            "server": {"port": 0},
            "routes": [
                {"name": "models", "prefix": "/ollama/", "upstream": f"http://localhost:{backend.server_address[1]}",
                 "methods": ["GET", "POST"], "allow": ["GET /api/tags", "POST /api/chat"]},
                {"name": "other", "prefix": "/other/", "upstream": f"http://localhost:{backend.server_address[1]}",
                 "allow": ["/*"]},
            ],
        }, {})
        store = KeyStore(work / "keys.json")
        key = store.add("demo", ["models"])
        audit = AuditLog(work / "audit.jsonl")
        server, _ = make_server(cfg, Gateway(cfg, store, audit))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        rows = [
            ("GET  /ollama/api/tags            (allowed)", call(port, "GET", "/ollama/api/tags", key)),
            ("GET  /ollama/api/ps              (not on the allowlist)", call(port, "GET", "/ollama/api/ps", key)),
            ("POST /ollama/api/pull            (built-in admin block)", call(port, "POST", "/ollama/api/pull", key)),
            ("GET  /ollama/api/tags            (no key)", call(port, "GET", "/ollama/api/tags")),
            ("GET  /other/x                    (key not scoped to it)", call(port, "GET", "/other/x", key)),
        ]
        for label, status in rows:
            print(f"{status}  {label}")
        server.shutdown()
        audit.close()
        print("audit lines:", len((work / "audit.jsonl").read_text().splitlines()))
    backend.shutdown()
    expected = [200, 403, 403, 401, 403]
    return 0 if [status for _, status in rows] == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
