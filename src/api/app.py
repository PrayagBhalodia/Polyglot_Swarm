"""The application: resolve a route, run the middleware chain, dispatch.

:class:`Application` is transport-agnostic — it turns a :class:`~api.http.Request`
into a :class:`~api.http.Response` and is driven either by the stdlib server or
directly by tests. :func:`build_app` is the composition root that wires the DB,
service, controllers, routes, and middleware together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from api.http import Handler, Middleware, Request, Response
from config.settings import Settings
from controllers.agent_controller import AgentController
from controllers.job_controller import JobController
from core.merger import MergeFn
from core.orchestrator import TranslateFn
from db.connection import Database
from middleware.errors import (
    MethodNotAllowedError,
    NotFoundError,
    error_middleware,
)
from middleware.json_body import json_body_middleware
from middleware.logging import logging_middleware
from routes.router import MethodNotAllowed, Resolved, Router
from routes.routes import build_router
from services.translation_service import TranslationService


class Application:
    """Holds the router and middleware chain; dispatches one request."""

    def __init__(
        self, router: Router, middleware: Sequence[Middleware] = ()
    ) -> None:
        self._router = router
        self._middleware = list(middleware)

    def dispatch(self, request: Request) -> Response:
        """Resolve, wrap with middleware, and invoke the matched handler."""
        handler: Handler = self._endpoint
        for mw in reversed(self._middleware):
            handler = _bind(mw, handler)
        return handler(request)

    def _endpoint(self, request: Request) -> Response:
        """Innermost handler: route resolution (raises into the error mw)."""
        match = self._router.resolve(request.method, request.path)
        if match is None:
            raise NotFoundError(f"no route for {request.method} {request.path}")
        if isinstance(match, MethodNotAllowed):
            raise MethodNotAllowedError(match.allowed)
        assert isinstance(match, Resolved)
        bound = replace(request, path_params=match.params)
        return match.handler(bound)


def _bind(mw: Middleware, nxt: Handler) -> Handler:
    def handler(request: Request) -> Response:
        return mw(request, nxt)

    return handler


def build_app(
    db: Database,
    *,
    settings: Settings,
    translate_fn: TranslateFn | None = None,
    merge_fn: MergeFn | None = None,
) -> Application:
    """Compose the whole API over an initialised :class:`Database`."""
    service = TranslationService(
        db,
        settings=settings,
        translate_fn=translate_fn,
        merge_fn=merge_fn,
    )
    jobs = JobController(service)
    agents = AgentController(service)
    router = build_router(jobs=jobs, agents=agents)
    middleware: list[Middleware] = [
        logging_middleware,   # outermost: sees the final status
        error_middleware,     # maps exceptions to JSON errors
        json_body_middleware,  # parses/validates request bodies
    ]
    return Application(router, middleware)
