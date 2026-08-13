"""Liveness endpoint — no dependencies, always cheap."""

from __future__ import annotations

from api.http import Request, Response


def health(request: Request) -> Response:
    """``GET /health`` → ``200`` with a small status document."""
    return Response(200, {"status": "ok", "service": "polyglot-swarm-api"})
