"""Tests for the Groq-backed translate / merge / repair seams.

A fake completion client stands in for the network, so these verify prompt
plumbing, fence-stripping, and failure handling with ZERO network access.
"""

from __future__ import annotations

import unittest

from models.agent import SwarmAgent
from models.enums import Language
from models.merge import MergeTask
from models.source import TranslationUnit
from models.verification import RepairRequest
from services.groq_brain import build_merge_fn, build_repair_fn, build_translate_fn
from services.groq_client import Completion


class FakeClient:
    """Records calls and returns a canned completion (or raises)."""

    def __init__(self, text: str = "def x(): pass", tokens: int = 11,
                 error: Exception | None = None) -> None:
        self.text = text
        self.tokens = tokens
        self.error = error
        self.calls: list[dict[str, str | None]] = []

    def complete(self, *, system: str, user: str, model: str | None = None) -> Completion:
        self.calls.append({"system": system, "user": user, "model": model})
        if self.error is not None:
            raise self.error
        return Completion(text=self.text, tokens=self.tokens)


def _unit() -> TranslationUnit:
    return TranslationUnit(
        job_id="j", source_file_id="f", index=0, content="MOVE 1 TO X",
        source_language=Language.COBOL, target_language=Language.PYTHON,
    )


def _agent() -> SwarmAgent:
    return SwarmAgent(name="w0", model="llama-3.3-70b-versatile")


class TranslateSeamTests(unittest.TestCase):
    def test_translates_and_records_tokens(self) -> None:
        client = FakeClient(text="def x():\n    pass", tokens=17)
        result = build_translate_fn(client)(_unit(), _agent())
        self.assertTrue(result.success)
        self.assertEqual(result.translated_content, "def x():\n    pass")
        self.assertEqual(result.tokens_used, 17)
        self.assertEqual(client.calls[0]["model"], "llama-3.3-70b-versatile")

    def test_strips_markdown_fences(self) -> None:
        client = FakeClient(text="```python\ndef x():\n    pass\n```")
        result = build_translate_fn(client)(_unit(), _agent())
        self.assertEqual(result.translated_content, "def x():\n    pass")

    def test_client_error_becomes_failed_result(self) -> None:
        client = FakeClient(error=RuntimeError("boom"))
        result = build_translate_fn(client)(_unit(), _agent())
        self.assertFalse(result.success)
        self.assertIn("boom", result.error or "")


class MergeSeamTests(unittest.TestCase):
    def _task(self) -> MergeTask:
        return MergeTask(
            source_file_id="f", target_language=Language.PYTHON,
            left="def a(): pass", right="def b(): pass",
            left_span=(0, 0), right_span=(1, 1),
        )

    def test_merges_two_pieces(self) -> None:
        client = FakeClient(text="def a(): pass\ndef b(): pass", tokens=5)
        result = build_merge_fn(client)(self._task(), _agent())
        self.assertTrue(result.success)
        self.assertIn("def a()", result.merged)
        self.assertIn("LEFT:", client.calls[0]["user"] or "")

    def test_client_error_becomes_failed_merge(self) -> None:
        client = FakeClient(error=RuntimeError("nope"))
        result = build_merge_fn(client)(self._task(), _agent())
        self.assertFalse(result.success)


class RepairSeamTests(unittest.TestCase):
    def _request(self) -> RepairRequest:
        return RepairRequest(
            source_file_id="f", target_language=Language.PYTHON,
            content="def a(:\n    pass", errors=("line 1: bad syntax",),
        )

    def test_returns_fixed_content(self) -> None:
        client = FakeClient(text="def a():\n    pass")
        fixed = build_repair_fn(client)(self._request(), _agent())
        self.assertEqual(fixed, "def a():\n    pass")

    def test_client_error_returns_original_unchanged(self) -> None:
        client = FakeClient(error=RuntimeError("down"))
        original = self._request()
        fixed = build_repair_fn(client)(original, _agent())
        self.assertEqual(fixed, original.content)


if __name__ == "__main__":
    unittest.main()
