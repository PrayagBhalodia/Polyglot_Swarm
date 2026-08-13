"""Request/response middleware for the Track B API.

Middleware are ``(request, next_handler) -> response`` callables composed around
the route handler. The application installs them outermost-first:

* :func:`~middleware.logging.logging_middleware` — records method/path/status,
* :func:`~middleware.errors.error_middleware` — maps exceptions to JSON errors,
* :func:`~middleware.json_body.json_body_middleware` — parses/validates bodies.
"""

from __future__ import annotations

from middleware.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    MethodNotAllowedError,
    NotFoundError,
    UnprocessableEntityError,
    UnsupportedMediaTypeError,
    error_middleware,
)
from middleware.json_body import json_body_middleware
from middleware.logging import logging_middleware

__all__ = [
    "ApiError",
    "BadRequestError",
    "ConflictError",
    "MethodNotAllowedError",
    "NotFoundError",
    "UnprocessableEntityError",
    "UnsupportedMediaTypeError",
    "error_middleware",
    "json_body_middleware",
    "logging_middleware",
]
