"""Contract tests for the model layer."""

from __future__ import annotations

import unittest

from models.enums import JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit, checksum


class LanguageTests(unittest.TestCase):
    def test_from_value_is_case_insensitive(self) -> None:
        self.assertIs(Language.from_value("  COBOL "), Language.COBOL)

    def test_from_value_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            Language.from_value("brainfuck")

    def test_from_value_accepts_common_aliases(self) -> None:
        self.assertIs(Language.from_value("C++"), Language.CPP)
        self.assertIs(Language.from_value("c#"), Language.CSHARP)
        self.assertIs(Language.from_value("JS"), Language.JAVASCRIPT)
        self.assertIs(Language.from_value("golang"), Language.GO)

    def test_famous_languages_are_present(self) -> None:
        for name in ("python", "javascript", "c", "cpp", "csharp", "go", "rust"):
            self.assertIsInstance(Language.from_value(name), Language)


class SourceFileTests(unittest.TestCase):
    def test_checksum_is_derived_when_absent(self) -> None:
        sf = SourceFile(path="a.cob", language=Language.COBOL, content="X")
        self.assertEqual(sf.sha256, checksum("X"))

    def test_line_count(self) -> None:
        sf = SourceFile(path="a.cob", language=Language.COBOL, content="a\nb\nc")
        self.assertEqual(sf.line_count, 3)

    def test_empty_path_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SourceFile(path="  ", language=Language.COBOL, content="x")

    def test_round_trip_dict(self) -> None:
        sf = SourceFile(path="a.cob", language=Language.COBOL, content="hello")
        self.assertEqual(SourceFile.from_dict(sf.to_dict()), sf)


class TranslationUnitTests(unittest.TestCase):
    def _unit(self, **kw: object) -> TranslationUnit:
        base = dict(
            job_id="j",
            source_file_id="f",
            index=0,
            content="MOVE 1 TO X",
            source_language=Language.COBOL,
            target_language=Language.PYTHON,
        )
        base.update(kw)
        return TranslationUnit(**base)  # type: ignore[arg-type]

    def test_same_language_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._unit(target_language=Language.COBOL)

    def test_negative_index_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._unit(index=-1)

    def test_bad_line_span_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._unit(start_line=10, end_line=5)

    def test_with_status_is_pure(self) -> None:
        unit = self._unit()
        updated = unit.with_status(UnitStatus.ASSIGNED, agent_id="agent-1")
        self.assertEqual(unit.status, UnitStatus.PENDING)  # original untouched
        self.assertEqual(updated.status, UnitStatus.ASSIGNED)
        self.assertEqual(updated.assigned_agent_id, "agent-1")

    def test_round_trip_dict(self) -> None:
        unit = self._unit(start_line=1, end_line=1)
        self.assertEqual(
            TranslationUnit.from_dict(unit.to_dict()).to_dict(), unit.to_dict()
        )


class ResultTests(unittest.TestCase):
    def test_failure_helper(self) -> None:
        r = TranslationResult.failure("u1", Language.PYTHON, "boom")
        self.assertFalse(r.success)
        self.assertEqual(r.error, "boom")
        self.assertEqual(r.translated_content, "")

    def test_success_with_error_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TranslationResult(
                unit_id="u",
                target_language=Language.PYTHON,
                translated_content="x",
                success=True,
                error="should not be here",
            )

    def test_failure_without_error_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TranslationResult(
                unit_id="u",
                target_language=Language.PYTHON,
                translated_content="",
                success=False,
            )


class JobTests(unittest.TestCase):
    def _job(self) -> TranslationJob:
        return TranslationJob(
            name="legacy-payroll",
            source_language=Language.COBOL,
            target_language=Language.PYTHON,
        )

    def test_same_language_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TranslationJob(
                name="x",
                source_language=Language.PYTHON,
                target_language=Language.PYTHON,
            )

    def test_progress_empty_is_zero(self) -> None:
        self.assertEqual(self._job().progress, 0.0)

    def test_progress_and_is_done(self) -> None:
        job = self._job()
        job.units = [
            TranslationUnit(
                job_id=job.id,
                source_file_id="f",
                index=i,
                content="X",
                source_language=Language.COBOL,
                target_language=Language.PYTHON,
            )
            for i in range(4)
        ]
        job.units[0].status = UnitStatus.TRANSLATED
        job.units[1].status = UnitStatus.TRANSLATED
        self.assertAlmostEqual(job.progress, 0.5)
        self.assertFalse(job.is_done)
        self.assertEqual(job.completed_units, 2)

        for u in job.units:
            u.status = UnitStatus.TRANSLATED
        self.assertTrue(job.is_done)
        self.assertEqual(job.progress, 1.0)

    def test_status_is_terminal(self) -> None:
        self.assertTrue(JobStatus.COMPLETED.is_terminal)
        self.assertFalse(JobStatus.TRANSLATING.is_terminal)

    def test_round_trip_dict(self) -> None:
        job = self._job()
        restored = TranslationJob.from_dict(job.to_dict())
        self.assertEqual(restored.id, job.id)
        self.assertEqual(restored.status, job.status)


if __name__ == "__main__":
    unittest.main()
