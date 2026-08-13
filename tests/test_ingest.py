"""Tests for source ingestion and the UI/ingest API surface.

Local-folder ingestion is exercised against a temp directory; the GitHub path
(which clones over the network) is intentionally not covered here so the suite
stays offline. All checks run with the stub Brain — no network, no API key.

The :class:`IngestPolicy` cases are the security net: ingestion is the one place
a request names a path the server then reads, so they check that an allow-list
root really does contain it — through ``..``, through symlinks, and through the
files a walk turns up.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from api.app import Application, build_app
from api.http import Request, Response
from config.settings import load_settings
from db.connection import Database
from models.enums import Language
from services.ingest import IngestPolicy, ingest_source


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


class IngestPolicyTests(unittest.TestCase):
    """``POLYGLOT_INGEST_ROOT`` must actually contain the walk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "allowed"
        self.outside = base / "secret"
        (self.root / "project").mkdir(parents=True)
        self.outside.mkdir()
        (self.root / "project" / "ok.cob").write_text(
            "MOVE 1 TO X\n", encoding="utf-8"
        )
        (self.outside / "private.cob").write_text(
            "MOVE 99 TO SECRET\n", encoding="utf-8"
        )
        self.policy = IngestPolicy(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ingest(self, location: str, policy: IngestPolicy | None = None) -> Any:
        return ingest_source(
            kind="local",
            location=location,
            source_language=Language.COBOL,
            policy=self.policy if policy is None else policy,
        )

    def test_path_inside_the_root_is_allowed(self) -> None:
        files = self._ingest(str(self.root / "project"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["path"].endswith("ok.cob"))

    def test_the_root_itself_is_allowed(self) -> None:
        self.assertEqual(len(self._ingest(str(self.root))), 1)

    def test_path_outside_the_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._ingest(str(self.outside))
        self.assertIn("outside the allowed ingest root", str(caught.exception))

    def test_dot_dot_traversal_is_rejected(self) -> None:
        traversal = str(self.root / "project" / ".." / ".." / "secret")
        with self.assertRaises(ValueError) as caught:
            self._ingest(traversal)
        self.assertIn("outside the allowed ingest root", str(caught.exception))

    def test_symlinked_directory_out_of_the_root_is_rejected(self) -> None:
        link = self.root / "escape"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks are unavailable here")
        with self.assertRaises(ValueError) as caught:
            self._ingest(str(link))
        self.assertIn("outside the allowed ingest root", str(caught.exception))

    def test_symlinked_file_out_of_the_root_is_skipped(self) -> None:
        link = self.root / "project" / "leak.cob"
        try:
            link.symlink_to(self.outside / "private.cob")
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks are unavailable here")
        files = self._ingest(str(self.root / "project"))
        self.assertEqual([f["path"] for f in files], ["ok.cob"])

    def test_unset_root_preserves_the_previous_behaviour(self) -> None:
        files = self._ingest(str(self.outside), policy=IngestPolicy())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["path"].endswith("private.cob"))

    def test_missing_path_is_still_a_plain_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._ingest(str(self.root / "nope"))
        self.assertIn("not a directory", str(caught.exception))


class IngestLimitTests(unittest.TestCase):
    def _tree(self, count: int, size: int = 12) -> tempfile.TemporaryDirectory[str]:
        tmp = tempfile.TemporaryDirectory()
        for i in range(count):
            Path(tmp.name, f"f{i:03d}.cob").write_text(
                "M" * size + "\n", encoding="utf-8"
            )
        return tmp

    def test_max_files_caps_the_batch(self) -> None:
        with self._tree(6) as name:
            files = ingest_source(
                kind="local",
                location=name,
                source_language=Language.COBOL,
                policy=IngestPolicy(max_files=4),
            )
        self.assertEqual(len(files), 4)

    def test_oversized_files_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            Path(name, "small.cob").write_text("MOVE 1 TO X\n", encoding="utf-8")
            Path(name, "huge.cob").write_text("M" * 5000 + "\n", encoding="utf-8")
            files = ingest_source(
                kind="local",
                location=name,
                source_language=Language.COBOL,
                policy=IngestPolicy(max_file_bytes=100),
            )
        self.assertEqual([f["path"] for f in files], ["small.cob"])

    def test_limits_are_configurable_from_the_environment(self) -> None:
        env = {"POLYGLOT_MAX_FILES": "7", "POLYGLOT_MAX_FILE_BYTES": "2048"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        policy = IngestPolicy.from_settings(settings)
        self.assertEqual(policy.max_files, 7)
        self.assertEqual(policy.max_file_bytes, 2048)
        self.assertIsNone(policy.root)

    def test_ingest_root_reaches_the_policy(self) -> None:
        with mock.patch.dict(
            os.environ, {"POLYGLOT_INGEST_ROOT": "/srv/code"}, clear=True
        ):
            settings = load_settings()
        self.assertEqual(settings.ingest_root, "/srv/code")
        self.assertEqual(IngestPolicy.from_settings(settings).root, Path("/srv/code"))


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

            run = self.app.dispatch(_request("POST", f"/jobs/{job['id']}/run?wait=1"))
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

    def test_ingest_outside_the_allowed_root_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            allowed = Path(base, "allowed")
            outside = Path(base, "outside")
            allowed.mkdir()
            outside.mkdir()
            Path(outside, "private.cob").write_text("MOVE 1 TO X\n", encoding="utf-8")

            app = build_app(
                self.db,
                settings=dataclasses.replace(
                    load_settings(), ingest_root=str(allowed)
                ),
            )
            response = app.dispatch(
                _request(
                    "POST",
                    "/jobs/ingest",
                    {
                        "source_kind": "local",
                        "location": str(outside),
                        "source_language": "cobol",
                        "target_language": "python",
                    },
                )
            )
        self.assertEqual(response.status, 400)
        self.assertIn(
            "outside the allowed ingest root", _json(response)["error"]["message"]
        )

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
