"""End-to-end API tests driven through ``Application.dispatch``.

These exercise the full Track B stack — middleware, routing, controllers, and
the service over a real (in-memory) Track A database — with the offline stub
Brain, so they need no sockets, network, or API key.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from urllib.parse import parse_qs, urlsplit

from api.app import Application, build_app
from api.http import Request, Response
from config.settings import load_settings
from db.connection import Database

_COBOL = "".join(f"MOVE {i} TO WS-COUNTER\n" for i in range(6))


def _request(
    method: str, path: str, body: Any | None = None
) -> Request:
    """Build a request, splitting any ``?query`` exactly as the server does."""
    raw = b""
    headers: dict[str, str] = {}
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    parsed = urlsplit(path)
    return Request(
        method=method,
        path=parsed.path,
        headers=headers,
        raw_body=raw,
        query=parse_qs(parsed.query),
    )


def _json(response: Response) -> Any:
    return json.loads(response.encode() or b"null")


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init_schema()
        settings = load_settings()
        self.app: Application = build_app(self.db, settings=settings)

    def tearDown(self) -> None:
        self.db.close()

    def _create_job(self) -> dict[str, Any]:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "payroll",
                    "source_language": "cobol",
                    "target_language": "python",
                    "source_files": [{"path": "payroll.cob", "content": _COBOL}],
                },
            )
        )
        self.assertEqual(response.status, 201)
        return _json(response)


class HealthAndRoutingTests(ApiTestCase):
    def test_health(self) -> None:
        response = self.app.dispatch(_request("GET", "/health"))
        self.assertEqual(response.status, 200)
        self.assertEqual(_json(response)["status"], "ok")

    def test_unknown_route_404(self) -> None:
        response = self.app.dispatch(_request("GET", "/nope"))
        self.assertEqual(response.status, 404)
        self.assertIn("error", _json(response))

    def test_method_not_allowed_405_with_allow_header(self) -> None:
        response = self.app.dispatch(_request("DELETE", "/jobs"))
        self.assertEqual(response.status, 405)
        self.assertEqual(response.headers.get("Allow"), "GET, POST")


class CreateJobTests(ApiTestCase):
    def test_create_returns_201_and_location(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "payroll",
                    "source_language": "cobol",
                    "target_language": "python",
                    "source_files": [{"path": "p.cob", "content": _COBOL}],
                },
            )
        )
        self.assertEqual(response.status, 201)
        body = _json(response)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(response.headers["Location"], f"/jobs/{body['id']}")

    def test_missing_field_is_400(self) -> None:
        response = self.app.dispatch(
            _request("POST", "/jobs", {"name": "x", "source_language": "cobol"})
        )
        self.assertEqual(response.status, 400)

    def test_empty_source_files_is_400(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "x",
                    "source_language": "cobol",
                    "target_language": "python",
                    "source_files": [],
                },
            )
        )
        self.assertEqual(response.status, 400)

    def test_unknown_language_is_400(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "x",
                    "source_language": "klingon",
                    "target_language": "python",
                    "source_files": [{"path": "p", "content": "x"}],
                },
            )
        )
        self.assertEqual(response.status, 400)

    def test_same_source_and_target_is_400(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "x",
                    "source_language": "python",
                    "target_language": "python",
                    "source_files": [{"path": "p", "content": "x"}],
                },
            )
        )
        self.assertEqual(response.status, 400)

    def test_non_json_content_type_is_415(self) -> None:
        request = Request(
            method="POST",
            path="/jobs",
            headers={"Content-Type": "text/plain"},
            raw_body=b"not json",
        )
        response = self.app.dispatch(request)
        self.assertEqual(response.status, 415)

    def test_malformed_json_is_400(self) -> None:
        request = Request(
            method="POST",
            path="/jobs",
            headers={"Content-Type": "application/json"},
            raw_body=b"{not valid",
        )
        response = self.app.dispatch(request)
        self.assertEqual(response.status, 400)


class JobLifecycleTests(ApiTestCase):
    def test_get_missing_job_is_404(self) -> None:
        response = self.app.dispatch(_request("GET", "/jobs/does-not-exist"))
        self.assertEqual(response.status, 404)

    def test_get_created_job(self) -> None:
        created = self._create_job()
        response = self.app.dispatch(_request("GET", f"/jobs/{created['id']}"))
        self.assertEqual(response.status, 200)
        self.assertEqual(_json(response)["id"], created["id"])

    def test_list_jobs(self) -> None:
        self._create_job()
        response = self.app.dispatch(_request("GET", "/jobs"))
        self.assertEqual(response.status, 200)
        body = _json(response)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["jobs"][0]["name"], "payroll")

    def test_run_completes_job_and_assembles_output(self) -> None:
        created = self._create_job()
        response = self.app.dispatch(
            _request("POST", f"/jobs/{created['id']}/run?wait=1")
        )
        self.assertEqual(response.status, 200)
        body = _json(response)
        self.assertEqual(body["job"]["status"], "completed")
        self.assertTrue(body["report"]["succeeded"])
        self.assertTrue(body["report"]["assembled_files"])
        self.assertGreater(body["report"]["total_tokens"], 0)

    def test_units_and_results_after_run(self) -> None:
        created = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{created['id']}/run?wait=1"))

        units = _json(self.app.dispatch(_request("GET", f"/jobs/{created['id']}/units")))
        self.assertGreater(units["count"], 0)
        self.assertTrue(all(u["status"] == "translated" for u in units["units"]))

        results = _json(
            self.app.dispatch(_request("GET", f"/jobs/{created['id']}/results"))
        )
        self.assertEqual(results["count"], units["count"])

    def test_running_twice_is_409(self) -> None:
        created = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{created['id']}/run?wait=1"))
        second = self.app.dispatch(_request("POST", f"/jobs/{created['id']}/run?wait=1"))
        self.assertEqual(second.status, 409)

    def test_run_missing_job_is_404(self) -> None:
        response = self.app.dispatch(_request("POST", "/jobs/nope/run?wait=1"))
        self.assertEqual(response.status, 404)


class AgentTests(ApiTestCase):
    def test_list_agents_matches_settings(self) -> None:
        response = self.app.dispatch(_request("GET", "/agents"))
        self.assertEqual(response.status, 200)
        body = _json(response)
        self.assertEqual(body["count"], load_settings().agent_count)
        self.assertTrue(all(a["status"] == "idle" for a in body["agents"]))


if __name__ == "__main__":
    unittest.main()
