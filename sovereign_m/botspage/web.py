"""Request handler for /bots. Pure function: no sockets, so JARVIS can call it and tests can too."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sovereign_m.boardio import default_board
from sovereign_m.botspage import actions, data
from sovereign_m.botspage.config import BY_ID

STATIC = Path(__file__).resolve().parent / "static" / "index.html"
MAX_BODY = 8192
_HDR = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def _json(code: int, obj) -> tuple[int, dict, bytes]:
    return code, {"Content-Type": "application/json", **_HDR}, json.dumps(obj, ensure_ascii=False).encode()


def _csrf_ok(headers: dict) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("x-m-bots") != "1" or "application/json" not in h.get("content-type", ""):
        return False
    origin, host = h.get("origin"), h.get("host")
    return not origin or urlsplit(origin).netloc == host


def is_bots_path(path: str) -> bool:
    p = path.split("?", 1)[0]
    return p == "/bots" or p.startswith("/bots/")


def handle(method: str, path: str, headers: dict, body: bytes = b"", board: Path | None = None,
           out_board: Path | None = None, model=None) -> tuple[int, dict, bytes]:
    board = board or default_board()
    out_board = out_board or board
    parts = urlsplit(path)
    p = parts.path.rstrip("/") or "/"
    q = parse_qs(parts.query)
    if method == "GET":
        if p == "/bots" and not parts.path.endswith("/"):
            return 301, {"Location": "/bots/", **_HDR}, b""
        if p == "/bots":
            return 200, {"Content-Type": "text/html; charset=utf-8", **_HDR}, STATIC.read_bytes()
        if p == "/bots/api/bots":
            return _json(200, {"bots": data.bot_list(board)})
        if p == "/bots/api/top":
            return _json(200, data.top(board, out_board))
        if p == "/bots/api/thread":
            t = data.thread(board, (q.get("bot") or [""])[0], out_board)
            return _json(200, t) if t else _json(404, {"error": "unknown bot"})
        if p == "/bots/daylog":
            date = (q.get("date") or [""])[0]
            text = data.daylog_text(board, date)
            if text is None:
                return 404, {"Content-Type": "text/plain", **_HDR}, b"no daylog"
            page = f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Daylog {html.escape(date)}</title><pre style='white-space:pre-wrap;font:16px/1.4 system-ui;padding:16px'>{html.escape(text)}</pre>"
            return 200, {"Content-Type": "text/html; charset=utf-8", **_HDR}, page.encode()
        return _json(404, {"error": "not found"})
    if method == "POST":
        if not _csrf_ok(headers):
            return _json(403, {"error": "forbidden"})
        if len(body) > MAX_BODY:
            return _json(413, {"error": "too large"})
        try:
            req = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return _json(400, {"error": "bad json"})
        if not isinstance(req, dict):
            return _json(400, {"error": "bad json"})
        if p == "/bots/api/message":
            if model is None:
                return _json(503, {"error": "no model configured"})
            res = actions.send_message(board, out_board, str(req.get("bot", "")), str(req.get("text", "")), model)
            return _json(200 if res.get("ok") else 400, res)
        m = re.fullmatch(r"/bots/api/suggestion/([0-9]{8}-[0-9]{6}-suggestion[0-9])/(approve|not-now)", p)
        if m:
            res = actions.decide(out_board, m.group(1), m.group(2))
            return _json(200 if res.get("ok") else 409, res)
        return _json(404, {"error": "not found"})
    return _json(405, {"error": "method not allowed"})
