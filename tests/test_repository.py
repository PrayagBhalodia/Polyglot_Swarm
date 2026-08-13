"""Tests for the SQLite data-access layer using in-memory databases."""

from __future__ import annotations

import unittest

from core.chunker import Chunker
from db.connection import Database
from db.repository import JobRepository, ResultRepository
from models.enums import JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile


def _job_with_units() -> TranslationJob:
    job = TranslationJob(
        name="payroll",
        source_language=Language.COBOL,
        target_language=Language.PYTHON,
        source_files=[
            SourceFile(
                path="payroll.cob",
                language=Language.COBOL,
                content="a\nb\nc\nd\n",
            )
        ],
    )
    job.units = Chunker(max_lines_per_unit=2).chunk_files(
        job.source_files, job_id=job.id, target_language=Language.PYTHON
    )
    return job


class JobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init_schema()
        self.repo = JobRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_save_and_get_round_trip(self) -> None:
        job = _job_with_units()
        self.repo.save(job)

        loaded = self.repo.get(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.id, job.id)
        self.assertEqual(loaded.name, job.name)
        self.assertEqual(len(loaded.source_files), 1)
        self.assertEqual(loaded.total_units, job.total_units)
        self.assertEqual([u.index for u in loaded.units], [0, 1])

    def test_save_is_idempotent_upsert(self) -> None:
        job = _job_with_units()
        self.repo.save(job)
        job.status = JobStatus.TRANSLATING
        job.units[0].status = UnitStatus.TRANSLATED
        self.repo.save(job)  # second save must not raise or duplicate

        loaded = self.repo.get(job.id)
        assert loaded is not None
        self.assertEqual(loaded.status, JobStatus.TRANSLATING)
        self.assertEqual(loaded.units[0].status, UnitStatus.TRANSLATED)
        self.assertEqual(self.repo.list_ids(), [job.id])

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.repo.get("does-not-exist"))


class ResultRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init_schema()
        self.job_repo = JobRepository(self.db)
        self.result_repo = ResultRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_save_and_get_result(self) -> None:
        job = _job_with_units()
        self.job_repo.save(job)  # units must exist for the FK
        unit = job.units[0]
        result = TranslationResult(
            unit_id=unit.id,
            target_language=Language.PYTHON,
            translated_content="print('a')",
            tokens_used=5,
            duration_ms=12,
        )
        self.result_repo.save(result)

        loaded = self.result_repo.get(unit.id)
        assert loaded is not None
        self.assertEqual(loaded.translated_content, "print('a')")
        self.assertTrue(loaded.success)
        self.assertEqual(loaded.tokens_used, 5)

    def test_failure_result_persists_error(self) -> None:
        job = _job_with_units()
        self.job_repo.save(job)
        unit = job.units[0]
        self.result_repo.save(
            TranslationResult.failure(unit.id, Language.PYTHON, "timeout")
        )
        loaded = self.result_repo.get(unit.id)
        assert loaded is not None
        self.assertFalse(loaded.success)
        self.assertEqual(loaded.error, "timeout")


if __name__ == "__main__":
    unittest.main()
