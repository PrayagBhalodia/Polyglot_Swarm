"""Tests for the minimal stdlib .env loader."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.dotenv import load_dotenv


class DotenvTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_loads_key_value_pairs(self) -> None:
        path = self._write("GROQ_API_KEY=gsk_abc123\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            load_dotenv(path)
            self.assertEqual(os.environ["GROQ_API_KEY"], "gsk_abc123")

    def test_does_not_override_real_environment(self) -> None:
        path = self._write("GROQ_API_KEY=from_file\n")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "from_shell"}, clear=True):
            load_dotenv(path)
            self.assertEqual(os.environ["GROQ_API_KEY"], "from_shell")

    def test_ignores_comments_blanks_and_export_and_quotes(self) -> None:
        path = self._write(
            "# a comment\n"
            "\n"
            "export FOO='quoted value'\n"
            "BAR = 42 \n"
            "not-a-pair\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            load_dotenv(path)
            self.assertEqual(os.environ["FOO"], "quoted value")
            self.assertEqual(os.environ["BAR"], "42")
            self.assertNotIn("not-a-pair", os.environ)

    def test_missing_file_is_a_noop(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            load_dotenv("/no/such/.env")  # must not raise
            self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
