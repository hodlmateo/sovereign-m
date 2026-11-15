"""Run the selftest against a throwaway local web server. Nothing leaves this machine.

    python examples/selftest/scan_local_demo.py

It starts a tiny HTTP server on the loopback address, scans only that port with
the built-in scanner, probes it with GET requests, and prints the findings.
The server counts every request method it sees, so you can check that only GET
arrived.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from sovereign_m.selftest.cli import run_scan
from sovereign_m.selftest.config import load_config
from sovereign_m.selftest.report import build_report, render_markdown

LOOPBACK = ".".join(("127", "0", "0", "1"))
methods: list[str] = []


class Demo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        methods.append("GET")
        if self.path == "/openapi.json":
            body = b'{"paths": {"/api/run-job": {"post": {}}}}'
        elif self.path == "/":
            body = b"<title>demo</title>hello"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # would be a bug in selftest if this ever ran
        methods.append("POST")
        self.send_response(500)
        self.end_headers()


def main() -> int:
    server = HTTPServer((LOOPBACK, 0), Demo)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cfg = load_config()
    cfg["scope"]["targets"] = ["loopback"]
    cfg["ports"].update({"range": str(port), "use_nmap": False})
    cfg["web"].update({"services": [], "requests_per_second": 200, "wordlist": ["openapi.json", "admin", "docs"]})

    findings, meta = run_scan(cfg, ["ports", "web"], allow_nmap=False)
    print(render_markdown(build_report(findings, meta)))
    print("methods the demo server saw:", sorted(set(methods)))
    server.shutdown()
    return 0 if set(methods) == {"GET"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
