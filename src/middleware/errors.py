"""API error types and the middleware that renders them as JSON.

Controllers raise :class:`ApiError` subclasses for client-facing failures; the
Track A core raises its own :class:`~core.errors.PolyglotSwarmError` hierarchy.
:func:`error_middleware` is the single place that turns *any* exception into a
consistent JSON error response, so no handler ever leaks a stack trace or an
unmapped 500.

Error body shape::

    {"error": {"status": 404, "message": "job 'abc' not found"}}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from api.http import Handler, Request, Response
from core.errors import (
    AssemblyError,
    ChunkingError,
    ConfigError,
    MergeError,
    OrchestrationError,
    PolyglotSwarmError,
    RepositoryError,
    VerificationError,
)


class ApiError(Exception):
    """An error with an HTTP status and a client-safe message."""

    status: int = 500

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        self.headers: Mapping[str, str] = headers or {}

    def to_response(self) -> Response:
        return Response(
            self.status,
            {"error": {"status": self.status, "message": self.message}},
            dict(self.headers),
        )


class BadRequestError(ApiError):
    status = 400


class NotFoundError(ApiError):
    status = 404


class MethodNotAllowedError(ApiError):
    status = 405

    def __init__(self, allowed: Sequence[str]) -> None:
        allow = ", ".join(allowed)
        super().__init__(
            f"method not allowed; try: {allow}",
            headers={"Allow": allow},
        )


class ConflictError(ApiError):
    status = 409


class UnsupportedMediaTypeError(ApiError):
    status = 415


class UnprocessableEntityError(ApiError):
    status = 422


# Track A domain errors → HTTP status. Anything not listed is a 500.
_CORE_ERROR_STATUS: dict[type[PolyglotSwarmError], int] = {
    OrchestrationError: 409,
    ChunkingError: 422,
    MergeError: 422,
    VerificationError: 422,
    AssemblyError: 422,
    ConfigError: 500,
    RepositoryError: 500,
}


def _status_for_core_error(exc: PolyglotSwarmError) -> int:
    for error_type, status in _CORE_ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return status
    return 500


def error_middleware(request: Request, next_handler: Handler) -> Response:
    """Catch every exception and render it as a JSON error response."""
    try:
        return next_handler(request)
    except ApiError as exc:
        return exc.to_response()
    except ValueError as exc:
        # Model/enum validation failures are the caller's fault → 400.
        return BadRequestError(str(exc)).to_response()
    except PolyglotSwarmError as exc:
        status = _status_for_core_error(exc)
        message = str(exc) if status != 500 else "internal server error"
        return Response(status, {"error": {"status": status, "message": message}})
    except Exception:  # noqa: BLE001 - last line of defence, never leak details
        return Response(
            500,
            {"error": {"status": 500, "message": "internal server error"}},
        )
