"""Middleware for the shared contract: handle(request, call_next) -> response.

request:  method, path, route, client_id, headers (lower-case), body
response: status, headers, body
Duck-typed; this module does not import the gateway. Plug in with
``sovereign_m.cache.middleware:make_middleware``.

Only the response body and content-type are stored. Request headers (auth
included) and the request itself are never written to the cache file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sovereign_m.cache.config import default_db_path, with_overrides
from sovereign_m.cache.policy import Policy
from sovereign_m.cache.store import ResponseCache, make_key


class CacheMiddleware:
    def __init__(self, config: dict[str, Any] | None = None, cache: ResponseCache | None = None) -> None:
        self.cfg = with_overrides(config)
        self.policy = Policy(self.cfg)
        self.endpoints = {str(e).rstrip("/") or "/" for e in self.cfg["endpoints"]}
        self.cache = cache or ResponseCache(
            default_db_path(self.cfg),
            ttl_seconds=float(self.cfg["ttl_seconds"]),
            max_bytes=int(float(self.cfg["max_mb"]) * 1024 * 1024),
            max_entries=int(self.cfg["max_entries"]),
        )

    def handle(self, request: Any, call_next):
        if str(request.method).upper() != "POST":
            return call_next(request)
        path = str(request.path).split("?", 1)[0].rstrip("/") or "/"
        if path not in self.endpoints:
            return call_next(request)
        try:
            payload = json.loads(request.body or b"")
        except (ValueError, UnicodeDecodeError):
            return self._bypass(request, call_next, "bad-json")
        ok, reason = self.policy.should_cache(payload, request.headers, request.route)
        if not ok:
            return self._bypass(request, call_next, reason)
        key = make_key(payload, route=str(request.route) + "|" + path)
        hit = self.cache.get(key)
        if hit is not None:
            body, ctype = hit
            return SimpleNamespace(status=200, headers={"content-type": ctype, "x-cache": "HIT"}, body=body)
        response = call_next(request)
        ctype = _header(response, "content-type")
        body = bytes(response.body or b"")
        if self.policy.cacheable_response(int(response.status), ctype, body) and _is_json(body):
            self.cache.put(key, body, ctype)
        elif int(response.status) != 200:
            self.cache.record_bypass("upstream-error")
        else:
            self.cache.record_bypass("response-not-json")
        _tag(response, "MISS")
        return response

    def _bypass(self, request: Any, call_next, reason: str):
        self.cache.record_bypass(reason)
        response = call_next(request)
        _tag(response, f"BYPASS({reason})")
        return response


def make_middleware(config: dict[str, Any] | None = None) -> CacheMiddleware:
    return CacheMiddleware(config)


def _header(response: Any, name: str) -> str:
    for k, v in (response.headers or {}).items():
        if str(k).lower() == name:
            return str(v)
    return ""


def _tag(response: Any, value: str) -> None:
    try:
        response.headers["x-cache"] = value
    except TypeError:
        response.headers = {**dict(response.headers or {}), "x-cache": value}


def _is_json(body: bytes) -> bool:
    try:
        json.loads(body)
        return True
    except (ValueError, UnicodeDecodeError):
        return False
