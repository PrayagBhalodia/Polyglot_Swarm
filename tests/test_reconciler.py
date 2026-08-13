"""Tests for the closing cross-file reconciliation pass.

This is the only phase that sees the whole translated codebase, so the
assertions are about exactly that: each file is shown the *other* files (never
itself), the pass is advisory rather than fatal, it cannot reorder or lose
files, and it runs before the verification gate so whatever it edits still has
to parse. All offline — the seam is a local stub or a fake.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from config.settings import load_settings
from core.chunker import Chunker
from core.errors import ReconcileError, VerificationError
from core.merger import MergedFile
from core.orchestrator import Orchestrator
from core.reconciler import Reconciler
from models.agent import SwarmAgent
from models.contract import Contract, ContractSymbol
from models.enums import AgentStatus, JobStatus, Language
from models.job import TranslationJob
from models.reconcile import FileSurface, ReconcileResult, ReconcileTask
from models.result import TranslationResult
from models.source import SourceFile
from services.groq_brain import build_reconcile_fn
from services.groq_client import Completion
from services.stub_contract import scan_declarations, stub_extract_contract
from services.stub_merger import stub_merge
from services.stub_reconciler import stub_reconcile
from services.verification import default_verify

_PAYROLL_SRC = "def calculate_net_pay(gross, tax):\n    return gross - tax\n"
_REPORTS_SRC = "def render_report(rows):\n    return calculate_net_pay(rows)\n"

# What the agents "emitted": reports.py kept snake_case for a symbol the
# contract agreed to camelCase — the classic independent-translator divergence.
_EMITTED = {
    "payroll.py": "func calculateNetPay(gross, tax) {\n  return gross - tax\n}\n",
    "reports.py": "func renderReport(rows) {\n  return calculate_net_pay(rows)\n}\n",
}


def _agents(n: int = 2) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


def _job(
    paths: tuple[str, ...] = ("payroll.py", "reports.py"),
    *,
    source: Language = Language.PYTHON,
    target: Language = Language.GO,
) -> TranslationJob:
    """A job whose file ids *are* their paths, so fixtures stay readable."""
    sources = {"payroll.py": _PAYROLL_SRC, "reports.py": _REPORTS_SRC}
    return TranslationJob(
        name="port",
        source_language=source,
        target_language=target,
        source_files=[
            SourceFile(path=p, language=source, content=sources[p], id=p)
            for p in paths
        ],
    )


def _translate_emitted(unit, agent):  # type: ignore[no-untyped-def]
    return TranslationResult(
        unit_id=unit.id,
        target_language=unit.target_language,
        translated_content=_EMITTED[unit.source_path],
        agent_id=agent.id,
        tokens_used=1,
    )


def _contract() -> Contract:
    return Contract(
        source_language=Language.PYTHON,
        target_language=Language.GO,
        symbols=(
            ContractSymbol(
                source_name="calculate_net_pay",
                target_name="calculateNetPay",
                source_path="payroll.py",
            ),
        ),
    )


def _merged(*paths: str) -> list[MergedFile]:
    return [
        MergedFile(
            source_file_id=p,
            content=_EMITTED[p],
            leaf_count=1,
            merge_count=0,
            depth=0,
            tokens_used=0,
        )
        for p in paths
    ]


class StubReconcilerTests(unittest.TestCase):
    def _task(self, content: str, contract: Contract | None = None) -> ReconcileTask:
        return ReconcileTask(
            source_file_id="f",
            source_path="reports.py",
            target_language=Language.GO,
            content=content,
            contract=_contract() if contract is None else contract,
        )

    def test_off_contract_spelling_is_rewritten(self) -> None:
        result = stub_reconcile(
            self._task("x := calculate_net_pay(1)\n"), SwarmAgent(name="a")
        )
        self.assertEqual(result.content, "x := calculateNetPay(1)\n")
        self.assertTrue(result.success)

    def test_other_casings_are_rewritten_too(self) -> None:
        content = "CalculateNetPay(); CALCULATE_NET_PAY;\n"
        result = stub_reconcile(self._task(content), SwarmAgent(name="a"))
        self.assertEqual(result.content, "calculateNetPay(); calculateNetPay;\n")

    def test_an_already_consistent_file_is_untouched(self) -> None:
        content = "x := calculateNetPay(1)\n"
        result = stub_reconcile(self._task(content), SwarmAgent(name="a"))
        self.assertEqual(result.content, content)
        self.assertEqual(result.tokens_used, 0)

    def test_the_pass_is_idempotent(self) -> None:
        once = stub_reconcile(
            self._task("calculate_net_pay()\n"), SwarmAgent(name="a")
        ).content
        twice = stub_reconcile(self._task(once), SwarmAgent(name="a")).content
        self.assertEqual(once, twice)

    def test_only_whole_words_are_rewritten(self) -> None:
        content = "calculate_net_pay_v2(); my_calculate_net_pay();\n"
        result = stub_reconcile(self._task(content), SwarmAgent(name="a"))
        self.assertEqual(result.content, content)

    def test_without_a_contract_nothing_is_guessed(self) -> None:
        empty = Contract(
            source_language=Language.PYTHON, target_language=Language.GO
        )
        content = "calculate_net_pay()\n"
        result = stub_reconcile(self._task(content, empty), SwarmAgent(name="a"))
        self.assertEqual(result.content, content)


class ReconcilerTests(unittest.TestCase):
    def _reconcile(self, fn, files=None, agents=None, concurrency=1):  # type: ignore[no-untyped-def]
        job = _job()
        merged = files if files is not None else _merged("payroll.py", "reports.py")
        return Reconciler(
            fn,
            agents or _agents(2),
            scanner=scan_declarations,
            max_concurrency=concurrency,
        ).reconcile(job, merged, contract=_contract())

    def test_each_file_sees_the_others_but_not_itself(self) -> None:
        seen: list[ReconcileTask] = []

        def spy(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            seen.append(task)
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content=task.content,
                agent_id=agent.id,
            )

        self._reconcile(spy)
        self.assertEqual(len(seen), 2)
        for task in seen:
            paths = [s.source_path for s in task.others]
            self.assertNotIn(task.source_path, paths)
            self.assertEqual(len(paths), 1)

    def test_surfaces_come_from_the_emitted_code(self) -> None:
        seen: list[ReconcileTask] = []

        def spy(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            seen.append(task)
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content=task.content,
            )

        self._reconcile(spy)
        by_path = {t.source_path: t for t in seen}
        # reports.py is told what payroll.py really declares — the emitted
        # camelCase name, not the source-language one.
        self.assertEqual(by_path["reports.py"].others[0].symbols, ("calculateNetPay",))

    def test_the_contract_is_passed_through(self) -> None:
        seen: list[ReconcileTask] = []

        def spy(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            seen.append(task)
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content=task.content,
            )

        self._reconcile(spy)
        self.assertTrue(all(t.contract is not None for t in seen))
        self.assertIn("calculateNetPay", seen[0].render_context())

    def test_a_single_file_is_skipped_entirely(self) -> None:
        calls = {"n": 0}

        def spy(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            calls["n"] += 1
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content=task.content,
            )

        files, results = self._reconcile(spy, files=_merged("payroll.py"))
        self.assertEqual(calls["n"], 0)
        self.assertEqual(results, [])
        self.assertEqual(len(files), 1)

    def test_changed_is_measured_not_believed(self) -> None:
        def liar(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            # Claims a change it did not make.
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content=task.content,
                changed=True,
            )

        _, results = self._reconcile(liar)
        self.assertTrue(all(not r.changed for r in results))

    def test_a_failed_pass_keeps_the_merged_content(self) -> None:
        def failing(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            return ReconcileResult.failure(
                task.source_file_id,
                task.target_language,
                "REPLACED WITH GARBAGE",  # must be ignored
                "brain exploded",
            )

        files, results = self._reconcile(failing)
        self.assertTrue(all(not r.success for r in results))
        self.assertEqual([f.content for f in files], list(_EMITTED.values()))
        self.assertTrue(all(not r.changed for r in results))

    def test_file_order_and_identity_survive(self) -> None:
        files, results = self._reconcile(stub_reconcile)
        self.assertEqual(
            [f.source_file_id for f in files], ["payroll.py", "reports.py"]
        )
        self.assertEqual(
            [r.source_file_id for r in results], ["payroll.py", "reports.py"]
        )

    def test_it_fixes_the_divergence(self) -> None:
        files, results = self._reconcile(stub_reconcile)
        self.assertIn("calculateNetPay(rows)", files[1].content)
        self.assertEqual([r.changed for r in results], [False, True])

    def test_requires_an_agent(self) -> None:
        with self.assertRaises(ReconcileError):
            Reconciler(stub_reconcile, [], scanner=scan_declarations)

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(ReconcileError):
            Reconciler(
                stub_reconcile, _agents(1), scanner=scan_declarations,
                max_concurrency=0,
            )

    def test_all_agents_offline_is_an_error(self) -> None:
        offline = _agents(2)
        for agent in offline:
            agent.status = AgentStatus.OFFLINE
        with self.assertRaises(ReconcileError):
            self._reconcile(stub_reconcile, agents=offline)


class ReconcileConcurrencyTests(unittest.TestCase):
    def test_files_are_reconciled_in_parallel_with_identical_output(self) -> None:
        lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0}

        def slow(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            try:
                time.sleep(0.02)
            finally:
                with lock:
                    state["in_flight"] -= 1
            return stub_reconcile(task, agent)

        job = _job()
        merged = _merged("payroll.py", "reports.py")

        sequential, _ = Reconciler(
            stub_reconcile, _agents(2), scanner=scan_declarations, max_concurrency=1
        ).reconcile(job, merged, contract=_contract())
        parallel, _ = Reconciler(
            slow, _agents(2), scanner=scan_declarations, max_concurrency=2
        ).reconcile(job, merged, contract=_contract())

        self.assertGreater(state["peak"], 1)
        self.assertEqual(
            [f.content for f in sequential], [f.content for f in parallel]
        )


class OrchestratorReconcilePathTests(unittest.TestCase):
    def _run(self, *, enabled: bool = True, paths=("payroll.py", "reports.py"),
             reconcile_fn=stub_reconcile, verify_fn=None):  # type: ignore[no-untyped-def]
        saved: list[JobStatus] = []

        class Spy:
            def save(self, job: TranslationJob) -> None:
                saved.append(job.status)

        job = _job(paths)
        report = Orchestrator(
            _agents(2),
            _translate_emitted,
            merge_fn=stub_merge,
            verify_fn=verify_fn,
            extract_contract_fn=stub_extract_contract,
            reconcile_fn=reconcile_fn if enabled else None,
            surface_scanner=scan_declarations,
            chunker=Chunker(max_lines_per_unit=20),
            persister=Spy(),
        ).run(job)
        return report, saved

    def test_lifecycle_routes_through_reconciling(self) -> None:
        report, saved = self._run()
        self.assertIn(JobStatus.RECONCILING, saved)
        self.assertLess(
            saved.index(JobStatus.MERGING), saved.index(JobStatus.RECONCILING)
        )
        self.assertTrue(report.succeeded)

    def test_the_divergence_is_fixed_in_the_assembled_output(self) -> None:
        report, _ = self._run()
        bodies = {f.source_path: f.content for f in report.assembled_files}
        self.assertIn("calculateNetPay(rows)", bodies["reports.py"])
        self.assertNotIn("calculate_net_pay", bodies["reports.py"])
        self.assertEqual(report.reconciled_files, 1)
        self.assertEqual(report.reconcile_failures, 0)

    def test_disabled_leaves_the_divergence_in_place(self) -> None:
        report, saved = self._run(enabled=False)
        bodies = {f.source_path: f.content for f in report.assembled_files}
        self.assertIn("calculate_net_pay(rows)", bodies["reports.py"])
        self.assertNotIn(JobStatus.RECONCILING, saved)
        self.assertEqual(report.reconciled_files, 0)

    def test_a_single_file_job_never_enters_the_phase(self) -> None:
        _, saved = self._run(paths=("payroll.py",))
        self.assertNotIn(JobStatus.RECONCILING, saved)

    def test_a_failing_pass_does_not_fail_the_job(self) -> None:
        def failing(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            return ReconcileResult.failure(
                task.source_file_id, task.target_language, task.content, "no brain"
            )

        report, _ = self._run(reconcile_fn=failing)
        self.assertTrue(report.succeeded)
        self.assertEqual(report.reconcile_failures, 2)
        self.assertEqual(report.reconciled_files, 0)

    def test_reconciliation_happens_before_the_gate(self) -> None:
        """Whatever this phase edits still has to parse."""

        def vandal(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
            return ReconcileResult(
                source_file_id=task.source_file_id,
                target_language=task.target_language,
                content="def broken(:\n",
                agent_id=agent.id,
            )

        # A Python target so ast.parse is the oracle and the damage is caught.
        job = _job(source=Language.COBOL, target=Language.PYTHON)
        orchestrator = Orchestrator(
            _agents(2),
            _translate_emitted,
            merge_fn=stub_merge,
            verify_fn=default_verify,
            extract_contract_fn=stub_extract_contract,
            reconcile_fn=vandal,
            surface_scanner=scan_declarations,
            chunker=Chunker(max_lines_per_unit=20),
        )
        with self.assertRaises(VerificationError):
            orchestrator.run(job)
        self.assertEqual(job.status, JobStatus.FAILED)


class GroqReconcileTests(unittest.TestCase):
    class _Client:
        def __init__(self, text: str = "func fixed() {}", boom: bool = False) -> None:
            self.text = text
            self.boom = boom
            self.user = ""

        def complete(
            self, *, system: str, user: str, model: str | None = None
        ) -> Completion:
            self.user = user
            if self.boom:
                raise RuntimeError("groq is down")
            return Completion(text=self.text, tokens=7)

    def _task(self) -> ReconcileTask:
        return ReconcileTask(
            source_file_id="f",
            source_path="reports.py",
            target_language=Language.GO,
            content="func renderReport() {}",
            others=(
                FileSurface(
                    source_file_id="g",
                    source_path="payroll.py",
                    symbols=("calculateNetPay",),
                ),
            ),
            contract=_contract(),
        )

    def test_prompt_carries_the_path_contract_and_neighbours(self) -> None:
        client = self._Client()
        build_reconcile_fn(client)(self._task(), SwarmAgent(name="a"))
        self.assertIn("reports.py", client.user)
        self.assertIn("payroll.py", client.user)
        self.assertIn("calculateNetPay", client.user)

    def test_a_failure_returns_the_file_unchanged(self) -> None:
        task = self._task()
        with self.assertLogs("polyglot.brain", level="WARNING"):
            result = build_reconcile_fn(self._Client(boom=True))(
                task, SwarmAgent(name="a")
            )
        self.assertFalse(result.success)
        self.assertEqual(result.content, task.content)
        self.assertIsNotNone(result.error)

    def test_code_fences_are_stripped(self) -> None:
        client = self._Client(text="```go\nfunc fixed() {}\n```")
        result = build_reconcile_fn(client)(self._task(), SwarmAgent(name="a"))
        self.assertEqual(result.content, "func fixed() {}")


class ReconcileSettingsTests(unittest.TestCase):
    def test_enabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(load_settings().reconcile_enabled)

    def test_env_can_disable_it(self) -> None:
        with mock.patch.dict(os.environ, {"POLYGLOT_RECONCILE": "off"}, clear=True):
            self.assertFalse(load_settings().reconcile_enabled)


if __name__ == "__main__":
    unittest.main()
