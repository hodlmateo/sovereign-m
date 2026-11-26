"""Glue for the JARVIS server (a ``BaseHTTPRequestHandler``): ``serve(handler, "GET"|"POST")``.

JARVIS imports this lazily, so a failure here can only break /bots, never the rest of the page.
"""

from __future__ import annotations

from sovereign_m.botspage import web
from sovereign_m.roundtable.model import HttpModel

_MODEL = HttpModel(timeout=120.0)


def serve(handler, method: str) -> None:
    n = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(min(n, web.MAX_BODY + 1)) if n else b""
    code, hdrs, out = web.handle(method, handler.path, dict(handler.headers), body, model=_MODEL)
    handler.send_response(code)
    for k, v in hdrs.items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(out)))
    handler.end_headers()
    handler.wfile.write(out)
