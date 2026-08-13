"""A stdlib-only HTTP server that drives an :class:`~api.app.Application`.

Uses :class:`http.server.HTTPServer` (single-threaded on purpose: the Track A
SQLite connection is not shared across threads, and this keeps request handling
trivially correct with zero dependencies). The handler is a thin adapter: it
reads the raw request, builds an :class:`~api.http.Request`, asks the app for a
:class:`~api.http.Response`, and writes it back as JSON.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from api.app import Application
from api.http import Request

_logger = logging.getLogger("polyglot.api")

# Methods the server is willing to read a body for / route.
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _make_handler(app: Application) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "PolyglotSwarmAPI/0.1"

        def _handle(self) -> None:
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b""
            request = Request(
                method=self.command,
                path=parsed.path,
                headers={k: v for k, v in self.headers.items()},
                raw_body=raw_body,
                query=parse_qs(parsed.query),
            )
            response = app.dispatch(request)
            payload = response.encode()

            self.send_response(response.status)
            if payload:
                self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        # Route every supported verb through the same adapter.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Silence the default stderr logging; logging_middleware handles it.
            return

    for method in _METHODS:
        setattr(_Handler, f"do_{method}", _Handler._handle)

    return _Handler


def serve(
    app: Application,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the blocking server until interrupted (Ctrl-C)."""
    handler = _make_handler(app)
    httpd = HTTPServer((host, port), handler)
    _logger.info("Polyglot Swarm API listening on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        _logger.info("shutting down")
    finally:
        httpd.server_close()
