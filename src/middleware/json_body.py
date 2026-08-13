"""Middleware that parses and validates JSON request bodies.

For methods that carry a body, it enforces a JSON content type and decodes the
payload once into :attr:`Request.json_body`, so controllers never deal with raw
bytes or content-type juggling. Malformed JSON becomes a ``400``; a non-JSON
content type becomes a ``415``.
"""

from __future__ import annotations

import json

from api.http import Handler, Request, Response
from middleware.errors import BadRequestError, UnsupportedMediaTypeError

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def json_body_middleware(request: Request, next_handler: Handler) -> Response:
    if request.method in _BODY_METHODS and request.raw_body:
        content_type = (request.header("content-type") or "").lower()
        if "application/json" not in content_type:
            raise UnsupportedMediaTypeError(
                "request body must be application/json"
            )
        try:
            request.json_body = json.loads(request.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequestError(f"invalid JSON body: {exc}") from exc
    return next_handler(request)
