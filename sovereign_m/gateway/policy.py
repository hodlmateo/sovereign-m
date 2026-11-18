"""Path rules: normalise a request path, then allowlist, denylist, built-in denies."""

from __future__ import annotations

from fnmatch import fnmatchcase
from urllib.parse import unquote

from sovereign_m.gateway.config import Route

# Model-server admin calls. Blocked on every route unless allow_admin is true.
ADMIN_DENY = (
    "/api/pull",
    "/api/delete",
    "/api/create",
    "/api/copy",
    "/api/push",
    "/api/blobs",
    "/api/blobs/*",
)
# Interactive docs and debug pages of app servers. Blocked unless allow_docs or allow_admin.
DOCS_DENY = (
    "/docs",
    "/docs/*",
    "/redoc",
    "/redoc/*",
    "/openapi.json",
    "/debug",
    "/debug/*",
)


def normalize(target: str) -> tuple[str, str, str]:
    """Split a request target into (decoded path, raw path, query).

    Returns an empty decoded path when the target is unusable: not an absolute
    path, a dot segment, an empty segment, an encoded slash or backslash, or a
    control character. Those are refused so the gateway and the upstream can
    never read the same path two different ways.
    """
    raw, _, query = target.partition("?")
    raw = raw.partition("#")[0]
    if not raw.startswith("/") or raw.startswith("//"):
        return "", raw, query
    lowered = raw.lower()
    if "%2f" in lowered or "%5c" in lowered or "%00" in lowered:
        return "", raw, query
    path = unquote(raw)
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "", raw, query
    segments = path.split("/")[1:]
    if any(seg in {".", ".."} for seg in segments):
        return "", raw, query
    if any(seg == "" for seg in segments[:-1]):
        return "", raw, query
    return path, raw, query


def match(pattern: str, path: str) -> bool:
    """A pattern ending in / is a prefix. Otherwise it is a glob (* crosses slashes)."""
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    return fnmatchcase(path, pattern)


def _candidates(*paths: str, fold: bool = True) -> list[str]:
    out: list[str] = []
    for path in paths:
        for variant in (path, path.rstrip("/") or "/"):
            for text in ((variant, variant.lower()) if fold else (variant,)):
                if text not in out:
                    out.append(text)
    return out


def refusal(route: Route, method: str, rest: str, full: str) -> str:
    """Return why a request is refused, or an empty string when it may go on."""
    cands = _candidates(rest, full)
    if not route.allow_admin and any(match(p, c) for p in ADMIN_DENY for c in cands):
        return "admin path"
    if not (route.allow_admin or route.allow_docs) and any(match(p, c) for p in DOCS_DENY for c in cands):
        return "docs or debug path"
    if any(match(p, c) for p in route.deny for c in cands):
        return "denied path"
    # Only the allowlist decides what may pass; it sees the path after the prefix.
    for allowed_method, pattern in route.allow:
        if allowed_method is not None and allowed_method != method and not (
            allowed_method == "GET" and method == "HEAD"
        ):
            continue
        if any(match(pattern, c) for c in _candidates(rest, fold=False)):
            return ""
    return "not in allowlist"
