"""A tiny, dependency-free path router.

Routes are registered as ``(method, template, handler)`` where a template is a
slash-delimited path whose ``{name}`` segments capture path parameters, e.g.
``/jobs/{id}/run``. :meth:`Router.resolve` returns a :class:`Resolved` match
(handler + captured params), a :class:`MethodNotAllowed` (path exists but not
for that method — a ``405`` with an ``Allow`` list), or ``None`` (no such path,
a ``404``). Matching is exact and order-independent; there is no precedence
magic to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.http import Handler


def _split(path: str) -> tuple[str, ...]:
    """Normalise a path/template into its non-empty segments."""
    return tuple(segment for segment in path.split("/") if segment)


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    template: tuple[str, ...]
    handler: Handler

    def match(self, segments: tuple[str, ...]) -> dict[str, str] | None:
        """Return captured params if ``segments`` fit this route, else ``None``."""
        if len(segments) != len(self.template):
            return None
        params: dict[str, str] = {}
        for tmpl, actual in zip(self.template, segments):
            if tmpl.startswith("{") and tmpl.endswith("}"):
                params[tmpl[1:-1]] = actual
            elif tmpl != actual:
                return None
        return params


@dataclass(frozen=True, slots=True)
class Resolved:
    """A successful match: the handler and the captured path parameters."""

    handler: Handler
    params: dict[str, str]


@dataclass(frozen=True, slots=True)
class MethodNotAllowed:
    """The path matched, but not for the requested method."""

    allowed: list[str]


class Router:
    """Registers routes and resolves an incoming (method, path)."""

    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, template: str, handler: Handler) -> None:
        self._routes.append(Route(method.upper(), _split(template), handler))

    def resolve(self, method: str, path: str) -> Resolved | MethodNotAllowed | None:
        segments = _split(path)
        allowed: list[str] = []
        for route in self._routes:
            params = route.match(segments)
            if params is None:
                continue
            if route.method == method.upper():
                return Resolved(route.handler, params)
            if route.method not in allowed:
                allowed.append(route.method)
        if allowed:
            return MethodNotAllowed(sorted(allowed))
        return None
