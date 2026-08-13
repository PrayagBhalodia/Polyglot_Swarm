"""Middleware that logs one line per request with its latency and status.

It sits outermost so it observes the *final* status — including responses that
``error_middleware`` produced from an exception. It never raises: logging must
not affect the response.
"""

from __future__ import annotations

import logging
import time

from api.http import Handler, Request, Response

_logger = logging.getLogger("polyglot.api")


def logging_middleware(request: Request, next_handler: Handler) -> Response:
    start = time.perf_counter()
    response = next_handler(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _logger.info(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.path,
        response.status,
        elapsed_ms,
    )
    return response
