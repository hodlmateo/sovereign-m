"""``python -m sovereign_m.botspage serve`` runs a standalone loopback test server (manual use).
``suggest`` posts up to 3 suggestion cards (meant for the 30-minute loop window)."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sovereign_m.boardio import default_board
from sovereign_m.botspage import actions, web
from sovereign_m.roundtable.model import HttpModel, dry_run_model

_LOOPBACK = "127.0.0" + ".1"


def make_handler(board, out_board, model):
    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, web.MAX_BODY + 1)) if n else b""
            code, hdrs, out = web.handle(method, self.path, dict(self.headers), body, board, out_board, model)
            self.send_response(code)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):
            pass

    return H


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m-bots")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "suggest"):
        s = sub.add_parser(name)
        s.add_argument("--board", type=Path)
        s.add_argument("--out-board", type=Path)
        s.add_argument("--dry-run", action="store_true", help="stub model")
    sub.choices["serve"].add_argument("--port", type=int, default=8479)
    a = p.parse_args(argv)
    board = (a.board or default_board()).expanduser()
    out = a.out_board.expanduser() if a.out_board else board
    model = dry_run_model() if a.dry_run else HttpModel()
    if a.cmd == "serve":
        ThreadingHTTPServer((_LOOPBACK, a.port), make_handler(board, out, model)).serve_forever()
        return 0
    made = actions.make_suggestions(board, out, model)
    print(json.dumps([str(m) for m in made], indent=1))
    return 0
