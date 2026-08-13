"""Tests for the merged-output verification gate and its repair loop.

The gate parses merged output and, when a repair seam is present, fixes failures
within a bounded budget. All of it runs with ZERO network access: the default
verifier is stdlib ``ast``; the repair seam is a deterministic stub.
"""

from __future__ import annotations

import unittest

from core.chunker import Chunker
from core.errors import VerificationError
from core.merger import Merger
from core.orchestrator import Orchestrator
from core.verifier import Verifier
from models.agent import SwarmAgent
from models.enums import JobStatus, Language
from models.job import TranslationJob
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit
from models.verification import RepairRequest
from services.verification import default_verify


def _agents(n: int = 2) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


def _job(content: str, target: Language = Language.PYTHON) -> TranslationJob:
    src = Language.COBOL if target != Language.COBOL else Language.PYTHON
    return TranslationJob(
        name="job",
        source_language=src,
        target_language=target,
        source_files=[SourceFile(path="f.src", language=src, content=content)],
    )


def _merge_join(task: MergeTask, agent: SwarmAgent) -> MergeResult:
    return MergeResult(
        source_file_id=task.source_file_id,
        target_language=task.target_language,
        merged=f"{task.left}\n{task.right}",
        agent_id=agent.id,
        tokens_used=1,
    )


def _merged_files(job: TranslationJob, contents: list[str]):
    """Build merged files directly from given per-chapter contents."""
    orch = Orchestrator(
        _agents(), _translate_from(contents), chunker=Chunker(max_lines_per_unit=1)
    )
    report = orch.run(job)  # naive path: fills results
    return Merger(_merge_join, _agents()).merge_job(job, report.results)


def _translate_from(contents: list[str]):
    def translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
        return TranslationResult(
            unit_id=unit.id,
            target_language=unit.target_language,
            translated_content=contents[unit.index],
            agent_id=agent.id,
            tokens_used=1,
        )

    return translate


class DefaultVerifyTests(unittest.TestCase):
    def test_valid_python_passes(self) -> None:
        ok, errors = default_verify("def f():\n    pass\n", Language.PYTHON)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_invalid_python_reports_line(self) -> None:
        ok, errors = default_verify("def f(:\n    pass\n", Language.PYTHON)
        self.assertFalse(ok)
        self.assertTrue(errors and "line" in errors[0])

    def test_empty_is_rejected(self) -> None:
        ok, errors = default_verify("   \n", Language.PYTHON)
        self.assertFalse(ok)

    def test_structural_check_for_non_python(self) -> None:
        ok, _ = default_verify("func main() { return }", Language.GO)
        self.assertTrue(ok)
        bad_ok, bad_errors = default_verify("func main() {", Language.GO)
        self.assertFalse(bad_ok)
        self.assertTrue(bad_errors)


class VerifierTests(unittest.TestCase):
    def test_passing_files_need_no_repair(self) -> None:
        job = _job("A\nB\n")
        merged = _merged_files(job, ["def a():\n    pass", "def b():\n    pass"])
        verified, results = Verifier(default_verify).verify(job, merged)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(sum(r.attempts for r in results), 0)
        self.assertEqual(len(verified), len(merged))

    def test_repair_fixes_a_broken_file(self) -> None:
        job = _job("A\n")
        merged = _merged_files(job, ["def a(:\n    pass"])  # syntactically broken

        def repair(request: RepairRequest, agent: SwarmAgent) -> str:
            # A real Groq fixer; here we just return valid Python.
            return "def a():\n    pass\n"

        verified, results = Verifier(
            default_verify, repair_fn=repair, agents=_agents(), max_attempts=2
        ).verify(job, merged)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].attempts, 1)
        self.assertIn("def a():", verified[0].content)

    def test_repair_budget_is_bounded(self) -> None:
        job = _job("A\n")
        merged = _merged_files(job, ["def a(:\n    pass"])
        calls = {"n": 0}

        def stubborn(request: RepairRequest, agent: SwarmAgent) -> str:
            calls["n"] += 1
            return "still broken ("  # never valid

        _, results = Verifier(
            default_verify, repair_fn=stubborn, agents=_agents(), max_attempts=3
        ).verify(job, merged)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].attempts, 3)
        self.assertEqual(calls["n"], 3)

    def test_repair_without_agents_is_rejected(self) -> None:
        with self.assertRaises(VerificationError):
            Verifier(default_verify, repair_fn=lambda r, a: "", agents=[])


class OrchestratorVerifyPathTests(unittest.TestCase):
    def test_pipeline_routes_through_verifying(self) -> None:
        saved: list[JobStatus] = []

        class Spy:
            def save(self, job: TranslationJob) -> None:
                saved.append(job.status)

        job = _job("A\nB\n")
        orch = Orchestrator(
            _agents(),
            _translate_from(["def a():\n    pass", "def b():\n    pass"]),
            merge_fn=_merge_join,
            verify_fn=default_verify,
            chunker=Chunker(max_lines_per_unit=1),
            persister=Spy(),
        )
        report = orch.run(job)
        self.assertTrue(report.succeeded)
        self.assertIn(JobStatus.VERIFYING, saved)
        self.assertTrue(report.verified)
        self.assertEqual(report.repairs, 0)

    def test_unrepairable_output_fails_the_job(self) -> None:
        job = _job("A\n")
        orch = Orchestrator(
            _agents(),
            _translate_from(["def a(:\n    pass"]),  # broken, no repair seam
            merge_fn=_merge_join,
            verify_fn=default_verify,
            chunker=Chunker(max_lines_per_unit=1),
        )
        with self.assertRaises(VerificationError):
            orch.run(job)
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_repair_seam_rescues_the_job(self) -> None:
        job = _job("A\n")

        def repair(request: RepairRequest, agent: SwarmAgent) -> str:
            return "def a():\n    pass\n"

        orch = Orchestrator(
            _agents(),
            _translate_from(["def a(:\n    pass"]),
            merge_fn=_merge_join,
            verify_fn=default_verify,
            repair_fn=repair,
            max_repair_attempts=2,
            chunker=Chunker(max_lines_per_unit=1),
        )
        report = orch.run(job)
        self.assertTrue(report.succeeded)
        self.assertTrue(report.verified)
        self.assertEqual(report.repairs, 1)
        self.assertIn("def a():", report.assembled_files[0].content)


if __name__ == "__main__":
    unittest.main()
