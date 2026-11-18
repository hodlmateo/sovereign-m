"""should_cache: decide whether a request may be stored or served from cache."""

from __future__ import annotations

import re
from typing import Any, Mapping

from sovereign_m.cache.config import with_overrides


class Policy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.cfg = with_overrides(config)
        self._markers = [
            re.compile(r"\b" + re.escape(str(m).strip()) + r"\b", re.IGNORECASE)
            for m in self.cfg["markers"] if str(m).strip()
        ]
        self._patterns = [re.compile(str(p)) for p in self.cfg["patterns"]]

    def should_cache(self, request: Any, headers: Mapping[str, str] | None = None, route: str = "") -> tuple[bool, str]:
        cfg = self.cfg
        h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if "no-store" in h.get("cache-control", "").lower():
            return False, "cache-control"
        if h.get("x-m-cache", "").strip().lower() == "off":
            return False, "header-off"
        if not isinstance(request, dict):
            return False, "not-json-object"
        if request.get("cache") is False:
            return False, "field-off"
        if route and route in cfg["never_cache_routes"]:
            return False, "route"
        if str(request.get("model", "")) in cfg["never_cache_models"]:
            return False, "model"
        if any(request.get(k) for k in ("tools", "functions", "tool_choice", "function_call")):
            return False, "tools"
        if request.get("stream") and not cfg["cache_streams"]:
            return False, "stream"
        temp = request.get("temperature")
        if temp is None and isinstance(request.get("options"), dict):
            temp = request["options"].get("temperature")
        if temp is None:
            temp = cfg.get("default_temperature")
        if temp is None:
            return False, "no-temperature"
        try:
            if isinstance(temp, bool) or float(temp) > float(cfg["temperature_max"]):
                return False, "temperature"
        except (TypeError, ValueError):
            return False, "temperature"
        text, has_image = _collect(request)
        if has_image:
            return False, "images"
        for rx in self._markers:
            if rx.search(text):
                return False, "personal-marker"
        for rx in self._patterns:
            if rx.search(text):
                return False, "personal-pattern"
        return True, "ok"

    def cacheable_response(self, status: int, content_type: str, body: bytes) -> bool:
        return status == 200 and "json" in content_type.lower() and bool(body)


def should_cache(request_json: Any, headers: Mapping[str, str] | None = None, config: dict[str, Any] | None = None) -> tuple[bool, str]:
    return Policy(config).should_cache(request_json, headers)


def _collect(request: dict) -> tuple[str, bool]:
    parts: list[str] = []
    image = bool(request.get("images"))

    def walk(value: Any) -> None:
        nonlocal image
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            if value.get("type") in {"image_url", "image", "input_image"} or "image_url" in value or "images" in value:
                image = True
            for k, v in value.items():
                if k not in {"type", "role"}:
                    walk(v)

    for key in ("prompt", "input", "system", "messages"):
        if key in request:
            walk(request[key])
    return "\n".join(parts), image
