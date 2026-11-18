"""Command line for cache. ``python -m sovereign_m.cache stats|purge|clear|demo``."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from sovereign_m.cache.config import default_db_path, load_config
from sovereign_m.cache.middleware import CacheMiddleware
from sovereign_m.cache.store import ResponseCache, format_stats

LOOPBACK = ".".join(("127", "0", "0", "1"))

USAGE = """
cache: a local response cache for model calls.

  python -m sovereign_m.cache stats [--db PATH]
  python -m sovereign_m.cache purge [--db PATH]    drop expired entries
  python -m sovereign_m.cache clear [--db PATH]    drop every stored response
  python -m sovereign_m.cache demo  [--db PATH]    dry run against a fake model on an ephemeral loopback port

The cache file holds model outputs (mode 0600). Without --db it lives under the workspace directory.
The same verbs work as: python -m sovereign_m cache ...
""".strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command, rest = args[0], args[1:]
    if command not in {"stats", "purge", "clear", "demo"}:
        print(USAGE)
        return 2
    db: str | None = None
    if rest:
        if len(rest) == 2 and rest[0] == "--db":
            db = rest[1]
        else:
            print(USAGE)
            return 2
    cfg = load_config()
    if command == "demo":
        return run_demo(Path(db) if db else None)
    path = Path(db) if db else default_db_path(cfg)
    if command != "clear" and not path.exists():
        print(f"no cache file at {path}")
        return 0
    with _open(path, cfg) as cache:
        if command == "stats":
            print(format_stats(cache.stats()))
        elif command == "purge":
            print(f"purged {cache.purge_expired()} expired entries")
        else:
            print(f"cleared {cache.clear()} entries")
    return 0


def _open(path: Path, cfg: dict) -> ResponseCache:
    return ResponseCache(
        path,
        ttl_seconds=float(cfg["ttl_seconds"]),
        max_bytes=int(float(cfg["max_mb"]) * 1024 * 1024),
        max_entries=int(cfg["max_entries"]),
    )


def console_main() -> None:
    sys.exit(main())


# ---- demo -------------------------------------------------------------

class _FakeModel(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        type(self).calls += 1
        body = json.dumps({"reply": f"fake answer #{type(self).calls}"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _proxy(base: str):
    def call_next(request):
        req = urllib.request.Request(base + request.path, data=request.body, method=request.method)
        req.add_header("content-type", request.headers.get("content-type", "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return SimpleNamespace(status=resp.status, headers={k.lower(): v for k, v in resp.headers.items()}, body=resp.read())
        except urllib.error.HTTPError as exc:
            return SimpleNamespace(status=exc.code, headers={k.lower(): v for k, v in exc.headers.items()}, body=exc.read())

    return call_next


def run_demo(db: Path | None = None) -> int:
    _FakeModel.calls = 0
    server = HTTPServer((LOOPBACK, 0), _FakeModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{LOOPBACK}:{server.server_address[1]}"
    tmp = None
    if db is None:
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "demo.sqlite"
    try:
        cache = ResponseCache(db, ttl_seconds=3600, max_bytes=1 << 20, max_entries=100)
        mw = CacheMiddleware({"cache_streams": False}, cache=cache)
        call_next = _proxy(base)
        sequence = [
            ("repeatable question", {"model": "demo", "temperature": 0, "messages": [{"role": "user", "content": "Explain what a hash map is."}]}, {}),
            ("same question again", {"model": "demo", "temperature": 0, "messages": [{"role": "user", "content": "  Explain what a  hash map is. "}]}, {}),
            ("different question", {"model": "demo", "temperature": 0, "messages": [{"role": "user", "content": "Explain what a queue is."}]}, {}),
            ("high temperature", {"model": "demo", "temperature": 0.9, "prompt": "Write a poem."}, {}),
            ("time-sensitive", {"model": "demo", "temperature": 0, "prompt": "What is the weather like?"}, {}),
            ("header opt-out", {"model": "demo", "temperature": 0, "prompt": "Explain recursion."}, {"x-m-cache": "off"}),
            ("repeatable question", {"model": "demo", "temperature": 0, "messages": [{"role": "user", "content": "Explain what a hash map is."}]}, {}),
        ]
        print(f"{'request':<24}{'x-cache':<28}upstream calls")
        for label, payload, headers in sequence:
            req = SimpleNamespace(
                method="POST", path="/v1/chat/completions", route="demo", client_id="demo",
                headers=dict(headers), body=json.dumps(payload).encode(),
            )
            resp = mw.handle(req, call_next)
            print(f"{label:<24}{resp.headers.get('x-cache', ''):<28}{_FakeModel.calls}")
        print()
        print(format_stats(cache.stats()))
        cache.close()
    finally:
        server.shutdown()
        server.server_close()
        if tmp is not None:
            tmp.cleanup()
    return 0
