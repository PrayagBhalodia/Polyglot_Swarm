"""Tests for the recursive, agent-driven chapter reconciliation.

These prove the merge tree behaves — order-preserving, log-depth, carry-safe,
and failure-loud — using a deterministic ``merge_fn`` stub standing in for the
Groq-backed reconciliation Brain, with ZERO network access.
"""

from __future__ import annotations

import math
import unittest

from core.chunker import Chunker
from core.errors import MergeError, OrchestrationError
from core.merger import Merger, assemble_merged
from core.orchestrator import Orchestrator
from models.agent import SwarmAgent
from models.enums import JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit


def _job(n_lines: int = 8) -> TranslationJob:
    content = "".join(f"MOVE {i} TO WS-COUNTER\n" for i in range(n_lines))
    return TranslationJob(
        name="payroll",
        source_language=Language.COBOL,
        target_language=Language.PYTHON,
        source_files=[
            SourceFile(path="payroll.cob", language=Language.COBOL, content=content)
        ],
    )


def _agents(n: int = 3) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


def _translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
    return TranslationResult(
        unit_id=unit.id,
        target_language=unit.target_language,
        translated_content=f"chapter {unit.index}",
        agent_id=agent.id,
        tokens_used=10,
        duration_ms=1,
    )


def _joining_merge(task: MergeTask, agent: SwarmAgent) -> MergeResult:
    """Deterministic reconciler: order-preserving join with a token cost."""
    return MergeResult(
        source_file_id=task.source_file_id,
        target_language=task.target_language,
        merged=f"{task.left}\n{task.right}",
        agent_id=agent.id,
        tokens_used=1,
        duration_ms=1,
    )


def _units_and_results(
    job: TranslationJob,
) -> tuple[TranslationJob, dict[str, TranslationResult]]:
    """Chunk + fake-translate a job so its units have results, without merging."""
    orch = Orchestrator(_agents(3), _translate, chunker=Chunker(max_lines_per_unit=1))
    report = orch.run(job)  # naive path (no merge_fn): fills results
    return job, report.results


class ReduceTests(unittest.TestCase):
    def test_single_chapter_needs_no_merge(self) -> None:
        job = _job(1)
        job, results = _units_and_results(job)
        merged = Merger(_joining_merge, _agents()).merge_job(job, results)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].leaf_count, 1)
        self.assertEqual(merged[0].merge_count, 0)
        self.assertEqual(merged[0].depth, 0)
        self.assertEqual(merged[0].content, "chapter 0")

    def test_merge_count_and_depth_for_n_leaves(self) -> None:
        # 8 lines at 1 line/unit -> 8 leaves -> 7 merges, depth ceil(log2(8))=3.
        job = _job(8)
        job, results = _units_and_results(job)
        merged = Merger(_joining_merge, _agents()).merge_job(job, results)[0]
        self.assertEqual(merged.leaf_count, 8)
        self.assertEqual(merged.merge_count, 7)
        self.assertEqual(merged.depth, math.ceil(math.log2(8)))
        self.assertEqual(merged.tokens_used, 7)  # one token per merge

    def test_order_is_preserved(self) -> None:
        job = _job(5)  # odd count exercises the carry-up path
        job, results = _units_and_results(job)
        merged = Merger(_joining_merge, _agents()).merge_job(job, results)[0]
        # Every chapter present, strictly in index order.
        positions = [merged.content.index(f"chapter {i}") for i in range(5)]
        self.assertEqual(positions, sorted(positions))

    def test_failed_merge_raises(self) -> None:
        def failing(task: MergeTask, agent: SwarmAgent) -> MergeResult:
            return MergeResult.failure(
                task.source_file_id, task.target_language, "brain exploded"
            )

        job = _job(4)
        job, results = _units_and_results(job)
        with self.assertRaises(MergeError):
            Merger(failing, _agents()).merge_job(job, results)

    def test_requires_an_agent(self) -> None:
        with self.assertRaises(MergeError):
            Merger(_joining_merge, [])


class AssembleMergedTests(unittest.TestCase):
    def test_wraps_merged_content_with_source_path(self) -> None:
        job = _job(4)
        job, results = _units_and_results(job)
        merged = Merger(_joining_merge, _agents()).merge_job(job, results)
        assembled = assemble_merged(job, merged)
        self.assertEqual(len(assembled), 1)
        self.assertEqual(assembled[0].source_path, "payroll.cob")
        self.assertEqual(assembled[0].unit_count, 4)
        self.assertEqual(assembled[0].content, merged[0].content)


class OrchestratorMergePathTests(unittest.TestCase):
    def test_pipeline_routes_through_merging(self) -> None:
        saved: list[JobStatus] = []

        class Spy:
            def save(self, job: TranslationJob) -> None:
                saved.append(job.status)

        job = _job(8)
        orch = Orchestrator(
            _agents(4),
            _translate,
            merge_fn=_joining_merge,
            chunker=Chunker(max_lines_per_unit=1),
            persister=Spy(),
        )
        report = orch.run(job)

        self.assertTrue(report.succeeded)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertIn(JobStatus.MERGING, saved)
        self.assertEqual(report.merge_count, 7)
        self.assertEqual(report.merge_depth, 3)
        # Translation-token accounting is unaffected by merging.
        self.assertEqual(report.total_tokens, 80)
        self.assertGreater(report.merge_tokens, 0)
        self.assertEqual(len(report.assembled_files), 1)

    def test_merge_failure_marks_job_failed(self) -> None:
        def failing(task: MergeTask, agent: SwarmAgent) -> MergeResult:
            return MergeResult.failure(
                task.source_file_id, task.target_language, "boom"
            )

        job = _job(4)
        orch = Orchestrator(
            _agents(2),
            _translate,
            merge_fn=failing,
            chunker=Chunker(max_lines_per_unit=1),
        )
        with self.assertRaises(MergeError):
            orch.run(job)
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_failed_translation_never_reaches_merge(self) -> None:
        def failing_translate(
            unit: TranslationUnit, agent: SwarmAgent
        ) -> TranslationResult:
            return TranslationResult.failure(
                unit.id, unit.target_language, "no groq"
            )

        job = _job(4)
        orch = Orchestrator(
            _agents(2),
            failing_translate,
            merge_fn=_joining_merge,
            chunker=Chunker(max_lines_per_unit=1),
        )
        with self.assertRaises(OrchestrationError):
            orch.run(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertTrue(any(u.status == UnitStatus.FAILED for u in job.units))


if __name__ == "__main__":
    unittest.main()
