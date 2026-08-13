"""Tests that the swarm actually swarms — and stays deterministic doing it.

Every seam here is a local fake that records how many calls were in flight at
once (a lock, a counter, and a very short sleep), so these prove real
concurrency with ZERO network access. The companion assertions are the
important half: parallel output must be byte-identical to sequential output,
with identical token accounting, because results are keyed by unit id and
assembled by index rather than by completion order.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from config.settings import load_settings
from core.chunker import Chunker
from core.errors import MergeError, OrchestrationError, VerificationError
from core.merger import MergeFn, Merger
from core.orchestrator import Orchestrator, RunReport, TranslateFn
from core.verifier import Verifier
from models.agent import SwarmAgent
from models.enums import Language
from models.job import TranslationJob
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit
from models.verification import RepairRequest

# Long enough that overlapping calls actually overlap, short enough to keep the
# suite fast; concurrency is asserted with a high-water mark, never with timing.
_DWELL_SECONDS = 0.02


class _Watcher:
    """Counts concurrent entries into a seam and remembers the peak."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.calls += 1
            self.peak = max(self.peak, self.in_flight)

    def leave(self) -> None:
        with self._lock:
            self.in_flight -= 1


def _job(n_lines: int = 8, *, name: str = "payroll") -> TranslationJob:
    content = "".join(f"MOVE {i} TO WS-COUNTER\n" for i in range(n_lines))
    return TranslationJob(
        name=name,
        source_language=Language.COBOL,
        target_language=Language.PYTHON,
        source_files=[
            SourceFile(path="payroll.cob", language=Language.COBOL, content=content)
        ],
    )


def _agents(n: int = 4) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


def _translate_with(watcher: _Watcher) -> TranslateFn:
    def translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
        watcher.enter()
        try:
            time.sleep(_DWELL_SECONDS)
        finally:
            watcher.leave()
        return TranslationResult(
            unit_id=unit.id,
            target_language=unit.target_language,
            translated_content=f"chapter {unit.index}",
            agent_id=agent.id,
            tokens_used=10,
            duration_ms=1,
        )

    return translate


def _merge_with(watcher: _Watcher) -> MergeFn:
    def merge(task: MergeTask, agent: SwarmAgent) -> MergeResult:
        watcher.enter()
        try:
            time.sleep(_DWELL_SECONDS)
        finally:
            watcher.leave()
        return MergeResult(
            source_file_id=task.source_file_id,
            target_language=task.target_language,
            merged=f"{task.left}\n{task.right}",
            agent_id=agent.id,
            tokens_used=1,
            duration_ms=1,
        )

    return merge


def _run(concurrency: int, watcher: _Watcher) -> RunReport:
    return Orchestrator(
        _agents(4),
        _translate_with(watcher),
        max_concurrency=concurrency,
        chunker=Chunker(max_lines_per_unit=1),
    ).run(_job(8))


class ConcurrentTranslationTests(unittest.TestCase):
    def test_units_translate_in_parallel(self) -> None:
        watcher = _Watcher()
        _run(4, watcher)
        self.assertEqual(watcher.calls, 8)
        self.assertGreater(watcher.peak, 1)

    def test_concurrency_of_one_stays_sequential(self) -> None:
        watcher = _Watcher()
        _run(1, watcher)
        self.assertEqual(watcher.peak, 1)

    def test_pool_is_bounded_by_max_concurrency(self) -> None:
        watcher = _Watcher()
        _run(2, watcher)
        self.assertLessEqual(watcher.peak, 2)

    def test_parallel_output_matches_sequential(self) -> None:
        sequential = _run(1, _Watcher())
        parallel = _run(4, _Watcher())

        self.assertEqual(
            [f.content for f in sequential.assembled_files],
            [f.content for f in parallel.assembled_files],
        )
        self.assertEqual(sequential.total_tokens, parallel.total_tokens)
        self.assertTrue(parallel.succeeded)

    def test_agents_are_released_after_the_phase(self) -> None:
        agents = _agents(3)
        Orchestrator(
            agents,
            _translate_with(_Watcher()),
            max_concurrency=3,
            chunker=Chunker(max_lines_per_unit=1),
        ).run(_job(6))
        self.assertTrue(all(a.is_available for a in agents))

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(OrchestrationError):
            Orchestrator(_agents(1), _translate_with(_Watcher()), max_concurrency=0)


class ConcurrentMergeTests(unittest.TestCase):
    def _merged_content(self, concurrency: int, watcher: _Watcher) -> str:
        job = _job(8)
        report = Orchestrator(
            _agents(4),
            _translate_with(_Watcher()),
            merge_fn=_merge_with(watcher),
            max_concurrency=concurrency,
            chunker=Chunker(max_lines_per_unit=1),
        ).run(job)
        self.assertEqual(len(report.merged_files), 1)
        return report.merged_files[0].content

    def test_a_tree_level_merges_in_parallel(self) -> None:
        watcher = _Watcher()
        self._merged_content(4, watcher)
        # 8 leaves -> 7 merges; the first level alone has 4 independent pairs.
        self.assertEqual(watcher.calls, 7)
        self.assertGreater(watcher.peak, 1)

    def test_merge_output_is_order_preserving_under_parallelism(self) -> None:
        sequential = self._merged_content(1, _Watcher())
        parallel = self._merged_content(4, _Watcher())
        self.assertEqual(sequential, parallel)
        positions = [parallel.index(f"chapter {i}") for i in range(8)]
        self.assertEqual(positions, sorted(positions))

    def test_merger_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(MergeError):
            Merger(_merge_with(_Watcher()), _agents(1), max_concurrency=0)


class ConcurrentVerifyTests(unittest.TestCase):
    def _job_of_three_files(self) -> TranslationJob:
        return TranslationJob(
            name="many",
            source_language=Language.COBOL,
            target_language=Language.PYTHON,
            source_files=[
                SourceFile(
                    path=f"f{i}.cob", language=Language.COBOL, content="MOVE 1 TO X\n"
                )
                for i in range(3)
            ],
        )

    def test_files_are_verified_in_parallel(self) -> None:
        watcher = _Watcher()

        def verify(content: str, language: Language) -> tuple[bool, list[str]]:
            watcher.enter()
            try:
                time.sleep(_DWELL_SECONDS)
            finally:
                watcher.leave()
            return True, []

        job = self._job_of_three_files()
        report = Orchestrator(
            _agents(3),
            _translate_with(_Watcher()),
            merge_fn=_merge_with(_Watcher()),
            verify_fn=verify,
            max_concurrency=3,
            chunker=Chunker(max_lines_per_unit=1),
        ).run(job)

        self.assertEqual(watcher.calls, 3)
        self.assertGreater(watcher.peak, 1)
        self.assertTrue(report.verified)

    def test_parallel_gate_reports_in_file_order(self) -> None:
        job = self._job_of_three_files()
        report = Orchestrator(
            _agents(2),
            _translate_with(_Watcher()),
            merge_fn=_merge_with(_Watcher()),
            verify_fn=lambda content, language: (True, []),
            max_concurrency=4,
            chunker=Chunker(max_lines_per_unit=1),
        ).run(job)
        self.assertEqual(
            [v.source_file_id for v in report.verifications],
            [m.source_file_id for m in report.merged_files],
        )

    def test_repair_agent_is_picked_before_the_pool_starts(self) -> None:
        """Repair must not race the round-robin cursor across threads."""
        seen: list[str] = []
        lock = threading.Lock()

        def repair(request: RepairRequest, agent: SwarmAgent) -> str:
            with lock:
                seen.append(agent.id)
            return "def fixed():\n    pass\n"

        job = self._job_of_three_files()
        report = Orchestrator(
            _agents(3),
            _translate_with(_Watcher()),
            merge_fn=_merge_with(_Watcher()),
            verify_fn=lambda content, language: ("fixed" in content, ["broken"]),
            repair_fn=repair,
            max_repair_attempts=1,
            max_concurrency=3,
            chunker=Chunker(max_lines_per_unit=1),
        ).run(job)

        self.assertTrue(report.verified)
        self.assertEqual(report.repairs, 3)
        # Three files, three distinct agents — no two files shared one.
        self.assertEqual(len(set(seen)), 3)

    def test_verifier_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(VerificationError):
            Verifier(lambda c, l: (True, []), max_concurrency=0)


class ConcurrencySettingsTests(unittest.TestCase):
    def test_defaults_to_agent_count(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.max_concurrency, settings.agent_count)

    def test_env_override_caps_concurrency(self) -> None:
        with mock.patch.dict(
            os.environ, {"POLYGLOT_MAX_CONCURRENCY": "3"}, clear=True
        ):
            settings = load_settings()
        self.assertEqual(settings.max_concurrency, 3)
        self.assertEqual(settings.agent_count, 8)

    def test_repair_budget_is_configurable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings().max_repair_attempts, 1)
        with mock.patch.dict(
            os.environ, {"POLYGLOT_MAX_REPAIR_ATTEMPTS": "0"}, clear=True
        ):
            self.assertEqual(load_settings().max_repair_attempts, 0)


if __name__ == "__main__":
    unittest.main()
