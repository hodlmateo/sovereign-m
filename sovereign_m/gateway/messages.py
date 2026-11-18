"""Request and response objects for the middleware contract (duck-typed).

Request:  method, path (after the route prefix, with query), route, client_id,
          headers (lower-case keys, auth headers already removed), body (bytes).
Response: status, headers (dict[str, str]), body (bytes).
A middleware is any object with ``handle(request, call_next) -> response``.
Duplicate response headers are joined with ", "; several Set-Cookie values are
joined with a newline and sent as separate headers.
"""

from __future__ import annotations

from typing import Any, Iterator


class Request:
    __slots__ = ("method", "path", "route", "client_id", "headers", "body")

    def __init__(self, method: str, path: str, route: str, client_id: str,
                 headers: dict[str, str], body: bytes = b"") -> None:
        self.method = method
        self.path = path
        self.route = route
        self.client_id = client_id
        self.headers = headers
        self.body = body


class Response:
    """``stream`` is only set when no middleware needs the whole body."""

    __slots__ = ("status", "headers", "body", "stream")

    def __init__(self, status: int, headers: dict[str, str] | None = None,
                 body: bytes = b"", stream: Iterator[bytes] | None = None) -> None:
        self.status = status
        self.headers = headers if headers is not None else {}
        self.body = body
        self.stream = stream


def coerce_response(value: Any) -> Response:
    """Accept any object that has status, headers and body."""
    if isinstance(value, Response):
        return value
    return Response(
        int(value.status),
        {str(k).lower(): str(v) for k, v in dict(value.headers).items()},
        bytes(value.body),
    )
