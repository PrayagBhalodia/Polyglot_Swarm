"""Tests for source ingestion and the UI/ingest API surface.

Local-folder ingestion is exercised against a temp directory; the GitHub path
(which clones over the network) is intentionally not covered here so the suite
stays offline. All checks run with the stub Brain — no network, no API key.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from api.app import Application, build_app
from api.http import Request, Response
from config.settings import load_settings
from db.connection import Database
from models.enums import Language
from services.ingest import ingest_source


def _request(method: str, path: str, body: Any | None = None) -> Request:
    raw = b""
    headers: dict[str, str] = {}
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return Request(method=method, path=path, headers=headers, raw_body=raw)


def _json(response: Response) -> Any:
    return json.loads(response.encode() or b"null")


class IngestSourceTests(unittest.TestCase):
    def _tree(self) -> tempfile.TemporaryDirectory[str]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "a.cob").write_text("MOVE 1 TO X\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.cbl").write_text("MOVE 2 TO Y\n", encoding="utf-8")
        (root / "notes.txt").write_text("ignore me", encoding="utf-8")
        return tmp

    def test_local_dir_picks_matching_files(self) -> None:
        with self._tree() as name:
            files = ingest_source(
                kind="local", location=name, source_language=Language.COBOL
            )
        paths = sorted(f["path"] for f in files)
        self.assertEqual(len(files), 2)  # the .txt is skipped
        self.assertTrue(any(p.endswith("b.cbl") for p in paths))

    def test_ingests_a_modern_source_language(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            Path(name, "main.go").write_text("package main\n", encoding="utf-8")
            Path(name, "util.rs").write_text("fn main() {}\n", encoding="utf-8")
            files = ingest_source(
                kind="local", location=name, source_language=Language.GO
            )
        self.assertEqual(len(files), 1)  # only the .go file
        self.assertTrue(files[0]["path"].endswith("main.go"))

    def test_no_matching_files_raises(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            Path(name, "x.txt").write_text("nope", encoding="utf-8")
            with self.assertRaises(ValueError):
                ingest_source(
                    kind="local", location=name, source_language=Language.COBOL
                )

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(ValueError):
            ingest_source(
                kind="local",
                location="/no/such/dir/here",
                source_language=Language.COBOL,
            )

    def test_bad_github_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            ingest_source(
                kind="github",
                location="http://evil.example/repo",
                source_language=Language.COBOL,
            )

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            ingest_source(
                kind="ftp", location="x", source_language=Language.COBOL
            )


class IngestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init_schema()
        self.app: Application = build_app(self.db, settings=load_settings())

    def tearDown(self) -> None:
        self.db.close()

    def test_ui_root_serves_html(self) -> None:
        response = self.app.dispatch(_request("GET", "/"))
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.content_type)
        self.assertIn(b"Polyglot Swarm", response.encode())

    def test_ingest_then_run_from_local_folder(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            Path(name, "pay.cob").write_text(
                "MOVE 1 TO X\nMOVE 2 TO Y\n", encoding="utf-8"
            )
            created = self.app.dispatch(
                _request(
                    "POST",
                    "/jobs/ingest",
                    {
                        "source_kind": "local",
                        "location": name,
                        "source_language": "cobol",
                        "target_language": "python",
                    },
                )
            )
            self.assertEqual(created.status, 201)
            job = _json(created)
            self.assertEqual(len(job["source_files"]), 1)

            run = self.app.dispatch(_request("POST", f"/jobs/{job['id']}/run"))
            self.assertEqual(run.status, 200)
            report = _json(run)["report"]
            self.assertTrue(report["succeeded"])
            self.assertTrue(report["verified"])
            self.assertTrue(report["assembled_files"])

    def test_ingest_bad_location_is_400(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs/ingest",
                {
                    "source_kind": "local",
                    "location": "/definitely/not/here",
                    "source_language": "cobol",
                    "target_language": "python",
                },
            )
        )
        self.assertEqual(response.status, 400)
        self.assertIn("error", _json(response))

    def test_ingest_missing_field_is_400(self) -> None:
        response = self.app.dispatch(
            _request(
                "POST",
                "/jobs/ingest",
                {"source_kind": "local", "location": "/tmp"},
            )
        )
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
