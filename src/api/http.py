"""Framework-agnostic HTTP request/response contracts for Track B.

These types are deliberately transport-independent: the same :class:`Request`
and :class:`Response` flow through the router, controllers, and middleware
whether they were produced by the stdlib server in :mod:`api.server` or built
directly in a test. Bodies are carried as already-decoded JSON values so the
handlers never touch raw bytes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# A parsed JSON document (object, array, scalar) or ``None`` when absent.
JsonValue = Any


@dataclass(slots=True)
class Request:
    """One inbound HTTP request, normalised for the handlers.

    ``json_body`` is populated by the JSON body middleware for requests that
    carry a body; controllers read it rather than parsing ``raw_body``
    themselves. ``path_params`` is filled in by the router once a route matches.
    """

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    raw_body: bytes = b""
    query: Mapping[str, list[str]] = field(default_factory=dict)
    path_params: Mapping[str, str] = field(default_factory=dict)
    json_body: JsonValue = None

    def header(self, name: str, default: str | None = None) -> str | None:
        """Case-insensitive header lookup (HTTP header names are not cased)."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    def query_one(self, name: str, default: str | None = None) -> str | None:
        """First value for a query parameter, or ``default`` if not present."""
        values = self.query.get(name)
        return values[0] if values else default


JSON_CONTENT_TYPE = "application/json; charset=utf-8"


@dataclass(slots=True)
class Response:
    """One outbound HTTP response.

    ``body`` is a JSON-serialisable value that the server serialises, or — for
    non-JSON responses such as the HTML UI — a raw ``str``/``bytes`` emitted
    verbatim (set ``content_type`` accordingly). ``None`` means an empty body.
    Extra ``headers`` are merged over the defaults.
    """

    status: int
    body: JsonValue = None
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str = JSON_CONTENT_TYPE

    def encode(self) -> bytes:
        """Serialise the body to UTF-8 bytes (empty for a ``None`` body).

        ``str``/``bytes`` bodies are treated as already-rendered payloads;
        anything else is JSON-encoded.
        """
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return json.dumps(self.body, ensure_ascii=False).encode("utf-8")


# A handler turns a request into a response; middleware wraps a handler.
Handler = Callable[[Request], Response]
Middleware = Callable[[Request, Handler], Response]
