"""Tests for the orchestration pipeline using a fake translate seam.

These prove Track A's coordination works end to end with ZERO network access:
the ``translate_fn`` is a local stub standing in for the Groq-backed Brain.
"""

from __future__ import annotations

import unittest

from core.chunker import Chunker
from core.errors import OrchestrationError
from core.orchestrator import Orchestrator
from models.agent import SwarmAgent
from models.enums import AgentStatus, JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit


def _job() -> TranslationJob:
    content = "".join(f"MOVE {i} TO WS-COUNTER\n" for i in range(9))
    return TranslationJob(
        name="payroll",
        source_language=Language.COBOL,
        target_language=Language.PYTHON,
        source_files=[
            SourceFile(path="payroll.cob", language=Language.COBOL, content=content)
        ],
    )


def _fake_translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
    """Deterministic stand-in for a real Groq translation."""
    return TranslationResult(
        unit_id=unit.id,
        target_language=unit.target_language,
        translated_content=f"# unit {unit.index} by {agent.name}\n",
        agent_id=agent.id,
        tokens_used=10,
        duration_ms=1,
    )


def _agents(n: int = 3) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


class HappyPathTests(unittest.TestCase):
    def test_full_pipeline_completes(self) -> None:
        job = _job()
        orch = Orchestrator(
            _agents(3), _fake_translate, chunker=Chunker(max_lines_per_unit=3)
        )
        report = orch.run(job)

        self.assertTrue(report.succeeded)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.total_units, 3)
        self.assertTrue(job.is_done)
        self.assertEqual(job.progress, 1.0)
        self.assertEqual(report.total_tokens, 30)

    def test_units_assigned_round_robin(self) -> None:
        job = _job()
        agents = _agents(3)
        orch = Orchestrator(
            agents, _fake_translate, chunker=Chunker(max_lines_per_unit=1)
        )
        report = orch.run(job)
        # 9 units across 3 agents -> each agent named in 3 assignments.
        used = [a.agent_id for a in report.assignments]
        for agent in agents:
            self.assertEqual(used.count(agent.id), 3)

    def test_agents_return_to_idle(self) -> None:
        job = _job()
        agents = _agents(2)
        Orchestrator(
            agents, _fake_translate, chunker=Chunker(max_lines_per_unit=3)
        ).run(job)
        for agent in agents:
            self.assertEqual(agent.status, AgentStatus.IDLE)
            self.assertIsNone(agent.current_unit_id)

    def test_assembled_output_ordered(self) -> None:
        job = _job()
        report = Orchestrator(
            _agents(2), _fake_translate, chunker=Chunker(max_lines_per_unit=3)
        ).run(job)
        self.assertEqual(len(report.assembled_files), 1)
        body = report.assembled_files[0].content
        # Chapters appear in index order.
        self.assertLess(body.index("unit 0"), body.index("unit 1"))
        self.assertLess(body.index("unit 1"), body.index("unit 2"))


class FailurePathTests(unittest.TestCase):
    def test_failed_unit_marks_job_failed(self) -> None:
        def failing(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
            return TranslationResult.failure(
                unit.id, unit.target_language, "groq exploded", agent_id=agent.id
            )

        job = _job()
        orch = Orchestrator(_agents(2), failing, chunker=Chunker(max_lines_per_unit=3))
        with self.assertRaises(OrchestrationError):
            orch.run(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertTrue(any(u.status == UnitStatus.FAILED for u in job.units))

    def test_rejects_non_pending_job(self) -> None:
        job = _job()
        job.status = JobStatus.COMPLETED
        with self.assertRaises(OrchestrationError):
            Orchestrator(_agents(1), _fake_translate).run(job)

    def test_rejects_job_without_source_files(self) -> None:
        job = TranslationJob(
            name="empty",
            source_language=Language.COBOL,
            target_language=Language.PYTHON,
        )
        with self.assertRaises(OrchestrationError):
            Orchestrator(_agents(1), _fake_translate).run(job)

    def test_requires_at_least_one_agent(self) -> None:
        with self.assertRaises(OrchestrationError):
            Orchestrator([], _fake_translate)


class PersistenceHookTests(unittest.TestCase):
    def test_persister_is_checkpointed(self) -> None:
        saved: list[JobStatus] = []

        class Spy:
            def save(self, job: TranslationJob) -> None:
                saved.append(job.status)

        job = _job()
        Orchestrator(
            _agents(2),
            _fake_translate,
            chunker=Chunker(max_lines_per_unit=3),
            persister=Spy(),
        ).run(job)
        # Every lifecycle stage should have been checkpointed at least once.
        for stage in (
            JobStatus.CHUNKING,
            JobStatus.DISPATCHED,
            JobStatus.TRANSLATING,
            JobStatus.ASSEMBLING,
            JobStatus.COMPLETED,
        ):
            self.assertIn(stage, saved)


if __name__ == "__main__":
    unittest.main()
