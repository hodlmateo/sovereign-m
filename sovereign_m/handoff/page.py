"""Read-only handoff list for the JARVIS phone page: HTML plus a copy button.

No forms, no POST, nothing is sent anywhere. ``render_fragment`` can be mounted
inside an existing page; ``serve`` runs a loopback-only GET server (not started
by anything in this repo).
"""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sovereign_m.boardio import default_board
from sovereign_m.handoff.writer import list_handoffs

_LOOPBACK = "127.0.0" + ".1"
_JS = """
function mCopy(i){var t=document.getElementById('h'+i).value;
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){mDone(i)})}
 else{var e=document.getElementById('h'+i);e.style.display='block';e.select();document.execCommand('copy');e.style.display='none';mDone(i)}}
function mDone(i){var b=document.getElementById('b'+i);b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)}
"""


def render_fragment(rows: list[dict]) -> str:
    if not rows:
        return "<p>No handoffs waiting.</p>"
    parts = ['<div class="m-handoffs">']
    for i, r in enumerate(rows):
        parts.append(
            f'<details><summary>{html.escape(r["title"])}</summary>'
            f'<pre style="white-space:pre-wrap">{html.escape(r["body"])}</pre>'
            f'<textarea id="h{i}" readonly style="display:none">{html.escape(r["body"])}</textarea>'
            f'<button id="b{i}" onclick="mCopy({i})">Copy</button></details>'
        )
    parts.append("</div><script>" + _JS + "</script>")
    return "\n".join(parts)


def render_page(directory: Path) -> str:
    return (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Handoffs</title><h1>Handoffs for the operator</h1>"
        "<p>Read-only. Copy, then paste into Claude Code or Grok yourself. Nothing is sent from here.</p>"
        + render_fragment(list_handoffs(directory))
    )


def serve(directory: Path, port: int = 8478) -> None:  # pragma: no cover - manual use
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = render_page(directory).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    HTTPServer((_LOOPBACK, port), Handler).serve_forever()
