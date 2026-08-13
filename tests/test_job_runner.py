"""Tests for asynchronous runs: 202, polling, and durable output.

Everything here uses the offline stub Brain (or a local fake), so there is no
network and no API key. The interesting property is the one a synchronous run
could never have: the HTTP response comes back immediately, progress climbs in
the database while the pipeline is still running, and the assembled output can
be read back from a *fresh* repository afterwards.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from api.app import Application, build_app
from api.http import Request, Response
from config.settings import Settings, load_settings
from db.connection import Database
from db.repository import OutputRepository
from models.agent import SwarmAgent
from models.enums import JobStatus
from models.result import TranslationResult
from models.source import TranslationUnit
from services.job_runner import build_job_runner

_COBOL = "".join(f"MOVE {i} TO WS-COUNTER\n" for i in range(4))

# Ceiling on how long a poll loop waits for a background run; generous so a
# loaded CI box cannot make these flaky, never actually reached in practice.
_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.01


def _request(method: str, path: str, body: Any | None = None) -> Request:
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


class AsyncRunTestCase(unittest.TestCase):
    """A file-backed database plus an app, torn down only once runs are idle."""

    settings_overrides: dict[str, Any] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "swarm.db")
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.settings: Settings = dataclasses.replace(
            load_settings(), database_path=self.db_path, **self.settings_overrides
        )
        self.app: Application = build_app(self.db, settings=self.settings)

    def tearDown(self) -> None:
        self._drain_runs()
        self.db.close()
        self._tmp.cleanup()

    # --- Helpers ------------------------------------------------------------

    @staticmethod
    def _drain_runs() -> None:
        """Join any live runner thread.

        A run marks the job terminal a moment *before* it writes its report and
        closes its connection, so tearing the temp database out from under it
        would be a race of the test's own making.
        """
        for thread in threading.enumerate():
            if thread.name.startswith("polyglot-job-"):
                thread.join(timeout=_TIMEOUT_SECONDS)

    def _create_job(self, content: str = _COBOL) -> str:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs",
                {
                    "name": "payroll",
                    "source_language": "cobol",
                    "target_language": "python",
                    "source_files": [{"path": "payroll.cob", "content": content}],
                },
            )
        )
        self.assertEqual(response.status, 201)
        job_id: str = _json(response)["id"]
        return job_id

    def _get_job(self, job_id: str) -> dict[str, Any]:
        response = self.app.dispatch(_request("GET", f"/jobs/{job_id}"))
        self.assertEqual(response.status, 200)
        body: dict[str, Any] = _json(response)
        return body

    def _poll_until(
        self, job_id: str, predicate: Any, what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        job = self._get_job(job_id)
        while not predicate(job):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}; last job was {job['status']}")
            time.sleep(_POLL_SECONDS)
            job = self._get_job(job_id)
        return job

    def _await_terminal(self, job_id: str) -> dict[str, Any]:
        return self._poll_until(
            job_id,
            lambda job: JobStatus(job["status"]).is_terminal,
            "a terminal job status",
        )


class AsyncRunTests(AsyncRunTestCase):
    def test_run_returns_202_immediately(self) -> None:
        job_id = self._create_job()
        response = self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))

        self.assertEqual(response.status, 202)
        body = _json(response)
        self.assertEqual(body["job_id"], job_id)
        self.assertTrue(body["accepted"])
        self.assertEqual(body["poll"], f"/jobs/{job_id}")
        self.assertEqual(response.headers["Location"], f"/jobs/{job_id}")

        self._await_terminal(job_id)

    def test_async_run_reaches_completed(self) -> None:
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))

        job = self._await_terminal(job_id)
        self.assertEqual(job["status"], JobStatus.COMPLETED.value)
        self.assertTrue(job["units"])
        self.assertTrue(all(u["status"] == "translated" for u in job["units"]))

    def test_output_is_available_once_completed(self) -> None:
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))
        self._await_terminal(job_id)

        response = self.app.dispatch(_request("GET", f"/jobs/{job_id}/output"))
        self.assertEqual(response.status, 200)
        body = _json(response)
        self.assertTrue(body["succeeded"])
        self.assertTrue(body["verified"])
        self.assertEqual(body["file_count"], 1)
        self.assertEqual(body["assembled_files"][0]["source_path"], "payroll.cob")
        self.assertTrue(body["assembled_files"][0]["content"].strip())
        self.assertGreater(body["total_tokens"], 0)
        self.assertEqual(body["progress"], 1.0)

    def test_output_before_a_run_is_409(self) -> None:
        job_id = self._create_job()
        response = self.app.dispatch(_request("GET", f"/jobs/{job_id}/output"))
        self.assertEqual(response.status, 409)
        self.assertIn("no output yet", _json(response)["error"]["message"])

    def test_output_for_unknown_job_is_404(self) -> None:
        response = self.app.dispatch(_request("GET", "/jobs/nope/output"))
        self.assertEqual(response.status, 404)

    def test_second_run_while_pending_is_409(self) -> None:
        job_id = self._create_job()
        self.assertEqual(
            self.app.dispatch(_request("POST", f"/jobs/{job_id}/run")).status, 202
        )
        second = self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))
        self.assertEqual(second.status, 409)
        self._await_terminal(job_id)

    def test_wait_query_still_returns_the_full_report(self) -> None:
        job_id = self._create_job()
        response = self.app.dispatch(_request("POST", f"/jobs/{job_id}/run?wait=1"))
        self.assertEqual(response.status, 200)
        report = _json(response)["report"]
        self.assertTrue(report["succeeded"])
        self.assertTrue(report["assembled_files"])

    def test_synchronous_run_also_persists_output(self) -> None:
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run?wait=1"))
        response = self.app.dispatch(_request("GET", f"/jobs/{job_id}/output"))
        self.assertEqual(response.status, 200)
        self.assertTrue(_json(response)["assembled_files"])


class OutputDurabilityTests(AsyncRunTestCase):
    def test_output_survives_a_fresh_repository(self) -> None:
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))
        self._await_terminal(job_id)

        # A brand new connection to the same file: nothing cached in process.
        with Database(self.db_path) as fresh:
            stored = OutputRepository(fresh).get(job_id)

        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.summary.succeeded)
        self.assertEqual(stored.summary.status, JobStatus.COMPLETED)
        self.assertEqual(len(stored.files), 1)
        self.assertEqual(stored.files[0].source_path, "payroll.cob")
        self.assertTrue(stored.files[0].content.strip())

    def test_saving_output_twice_replaces_the_previous_files(self) -> None:
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run?wait=1"))
        outputs = OutputRepository(self.db)
        first = outputs.get(job_id)
        self.assertIsNotNone(first)
        assert first is not None

        # A re-run must overwrite, never accumulate stale files alongside.
        outputs.save(
            dataclasses.replace(first.summary, total_tokens=999),
            [dataclasses.replace(first.files[0], content="def rerun():\n    pass\n")],
        )
        second = outputs.get(job_id)
        assert second is not None
        self.assertEqual(len(second.files), 1)
        self.assertEqual(second.summary.total_tokens, 999)
        self.assertIn("rerun", second.files[0].content)


class ProgressTests(AsyncRunTestCase):
    """Progress must climb *during* the run, not only at the end."""

    settings_overrides = {"max_concurrency": 1, "max_lines_per_unit": 1}

    def setUp(self) -> None:
        super().setUp()
        self.started = threading.Semaphore(0)
        self.release = threading.Semaphore(0)
        self.app = build_app(
            self.db, settings=self.settings, translate_fn=self._gated_translate
        )

    def _gated_translate(
        self, unit: TranslationUnit, agent: SwarmAgent
    ) -> TranslationResult:
        self.started.release()
        self.release.acquire()
        return TranslationResult(
            unit_id=unit.id,
            target_language=unit.target_language,
            translated_content=f"def chapter_{unit.index}():\n    pass\n",
            agent_id=agent.id,
            tokens_used=1,
        )

    def test_progress_advances_while_the_job_runs(self) -> None:
        job_id = self._create_job()  # 4 lines at 1 line/unit -> 4 units
        self.assertEqual(
            self.app.dispatch(_request("POST", f"/jobs/{job_id}/run")).status, 202
        )

        # Let exactly two of the four units through, then observe progress from
        # this thread while the worker is still blocked on the third.
        for _ in range(2):
            self.assertTrue(self.started.acquire(timeout=_TIMEOUT_SECONDS))
            self.release.release()

        mid = self._poll_until(
            job_id, lambda job: job["progress"] > 0.0, "partial progress"
        )
        self.assertLess(mid["progress"], 1.0)
        self.assertEqual(mid["status"], JobStatus.TRANSLATING.value)

        for _ in range(2):
            self.assertTrue(self.started.acquire(timeout=_TIMEOUT_SECONDS))
            self.release.release()

        done = self._await_terminal(job_id)
        self.assertEqual(done["status"], JobStatus.COMPLETED.value)
        self.assertEqual(done["progress"], 1.0)


class FailedRunTests(AsyncRunTestCase):
    def _exploding_translate(
        self, unit: TranslationUnit, agent: SwarmAgent
    ) -> TranslationResult:
        return TranslationResult.failure(
            unit.id, unit.target_language, "groq exploded", agent_id=agent.id
        )

    def test_failure_is_recorded_and_readable(self) -> None:
        self.app = build_app(
            self.db, settings=self.settings, translate_fn=self._exploding_translate
        )
        job_id = self._create_job()
        self.app.dispatch(_request("POST", f"/jobs/{job_id}/run"))

        job = self._await_terminal(job_id)
        self.assertEqual(job["status"], JobStatus.FAILED.value)
        self._drain_runs()  # the reason is written just after the status flips

        response = self.app.dispatch(_request("GET", f"/jobs/{job_id}/output"))
        self.assertEqual(response.status, 200)
        body = _json(response)
        self.assertFalse(body["succeeded"])
        self.assertEqual(body["assembled_files"], [])
        self.assertIn("failed to translate", body["error"])


class JobRunnerUnitTests(AsyncRunTestCase):
    def test_worker_uses_its_own_connection(self) -> None:
        runner = build_job_runner(self.db, settings=self.settings)
        job_id = self._create_job()

        self.assertTrue(runner.start(job_id))
        self.assertTrue(runner.wait(job_id, timeout=_TIMEOUT_SECONDS))
        self.assertIsNone(runner.error_for(job_id))
        # The request-thread connection is still perfectly usable afterwards.
        self.assertEqual(
            self._get_job(job_id)["status"], JobStatus.COMPLETED.value
        )

    def test_start_is_idempotent_while_running(self) -> None:
        runner = build_job_runner(self.db, settings=self.settings)
        job_id = self._create_job()
        self.assertTrue(runner.start(job_id))
        # Either the first run is still in flight (False) or it already
        # finished; in both cases the job must end COMPLETED exactly once.
        runner.start(job_id)
        self.assertTrue(runner.wait(job_id, timeout=_TIMEOUT_SECONDS))
        self.assertEqual(
            self._get_job(job_id)["status"], JobStatus.COMPLETED.value
        )

    def test_unknown_job_is_reported_not_crashed(self) -> None:
        runner = build_job_runner(self.db, settings=self.settings)
        self.assertTrue(runner.start("no-such-job"))
        self.assertTrue(runner.wait("no-such-job", timeout=_TIMEOUT_SECONDS))
        error = runner.error_for("no-such-job")
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("disappeared", error)


class SiblingConnectionTests(unittest.TestCase):
    def test_in_memory_siblings_share_one_database(self) -> None:
        with Database(":memory:") as db:
            db.init_schema()
            with db.sibling() as other:
                other.connection.execute(
                    "INSERT INTO jobs VALUES ('j', 'n', 'cobol', 'python',"
                    " 'pending', '2020-01-01T00:00:00', '2020-01-01T00:00:00')"
                )
                other.connection.commit()
            rows = db.connection.execute("SELECT id FROM jobs").fetchall()
        self.assertEqual([r["id"] for r in rows], ["j"])

    def test_file_siblings_share_one_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "s.db")
            with Database(path) as db:
                db.init_schema()
                sibling = db.sibling()
                self.assertEqual(sibling.path, path)
                sibling.close()


if __name__ == "__main__":
    unittest.main()
