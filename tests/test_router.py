"""Unit tests for the path router's matching and method handling."""

from __future__ import annotations

import unittest

from api.http import Request, Response
from routes.router import MethodNotAllowed, Resolved, Router


def _ok(request: Request) -> Response:
    return Response(200, {"params": dict(request.path_params)})


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = Router()
        self.router.add("GET", "/health", _ok)
        self.router.add("GET", "/jobs", _ok)
        self.router.add("POST", "/jobs", _ok)
        self.router.add("GET", "/jobs/{id}", _ok)
        self.router.add("POST", "/jobs/{id}/run", _ok)

    def test_static_match(self) -> None:
        match = self.router.resolve("GET", "/health")
        self.assertIsInstance(match, Resolved)

    def test_captures_path_params(self) -> None:
        match = self.router.resolve("GET", "/jobs/abc123")
        assert isinstance(match, Resolved)
        self.assertEqual(match.params, {"id": "abc123"})

    def test_nested_param_route(self) -> None:
        match = self.router.resolve("POST", "/jobs/xy/run")
        assert isinstance(match, Resolved)
        self.assertEqual(match.params, {"id": "xy"})

    def test_trailing_slash_is_ignored(self) -> None:
        self.assertIsInstance(self.router.resolve("GET", "/jobs/"), Resolved)

    def test_unknown_path_returns_none(self) -> None:
        self.assertIsNone(self.router.resolve("GET", "/nope"))

    def test_wrong_method_reports_allowed(self) -> None:
        match = self.router.resolve("DELETE", "/jobs")
        assert isinstance(match, MethodNotAllowed)
        self.assertEqual(match.allowed, ["GET", "POST"])

    def test_segment_count_must_match(self) -> None:
        # "/jobs/a/b" has three segments; no route has that shape.
        self.assertIsNone(self.router.resolve("GET", "/jobs/a/b"))


if __name__ == "__main__":
    unittest.main()
