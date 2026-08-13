"""Routing for the Track B API: the :class:`Router` and the route table.

:mod:`routes.router` is the matching engine (method + templated path → handler);
:mod:`routes.routes` is the declarative table that wires each URL to a
controller method.
"""

from __future__ import annotations

from routes.router import MethodNotAllowed, Resolved, Route, Router
from routes.routes import build_router

__all__ = [
    "MethodNotAllowed",
    "Resolved",
    "Route",
    "Router",
    "build_router",
]
