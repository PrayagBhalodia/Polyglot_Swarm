"""Tests for the contract-first pass — the cross-file coherence mechanism.

The point of the contract is that agents stop inventing names independently, so
the assertions here are about *agreement*: the extractor is deterministic and
offline, the same table reaches every translate and merge call, the lifecycle
routes through ANALYZING, and turning the flag off restores the naive path
exactly as it was.
"""

from __future__ import annotations

import dataclasses
import json
import os
import unittest
from collections.abc import Sequence
from unittest import mock

from config.settings import load_settings
from core.chunker import Chunker
from core.errors import ConfigError
from core.orchestrator import Orchestrator
from models.agent import SwarmAgent
from models.contract import Contract, ContractSymbol
from models.enums import JobStatus, Language
from models.job import TranslationJob
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit
from services.groq_brain import build_extract_contract_fn, build_translate_fn
from services.groq_client import Completion
from services.stub_contract import stub_extract_contract

_PAYROLL = """\
def calculate_net_pay(gross, tax):
    return gross - tax


class PayrollLedger:
    def total(self):
        return 0


def _private_helper():
    return None
"""

_REPORTS = """\
def render_report(rows):
    return "\\n".join(rows)
"""


def _agents(n: int = 2) -> list[SwarmAgent]:
    return [SwarmAgent(name=f"agent-{i}") for i in range(n)]


def _files() -> list[SourceFile]:
    return [
        SourceFile(path="payroll.py", language=Language.PYTHON, content=_PAYROLL),
        SourceFile(path="reports.py", language=Language.PYTHON, content=_REPORTS),
    ]


def _job(files: list[SourceFile] | None = None) -> TranslationJob:
    return TranslationJob(
        name="port",
        source_language=Language.PYTHON,
        target_language=Language.GO,
        source_files=files if files is not None else _files(),
    )


class StubExtractionTests(unittest.TestCase):
    def _contract(self, target: Language = Language.GO) -> Contract:
        return stub_extract_contract(_files(), Language.PYTHON, target)

    def test_finds_public_functions_and_classes(self) -> None:
        names = {s.source_name for s in self._contract().symbols}
        self.assertEqual(
            names, {"calculate_net_pay", "PayrollLedger", "render_report"}
        )

    def test_private_symbols_are_left_out(self) -> None:
        self.assertNotIn(
            "_private_helper",
            {s.source_name for s in self._contract().symbols},
        )

    def test_target_names_follow_the_target_convention(self) -> None:
        by_source = {s.source_name: s for s in self._contract(Language.GO).symbols}
        self.assertEqual(by_source["calculate_net_pay"].target_name, "calculateNetPay")
        self.assertEqual(by_source["PayrollLedger"].target_name, "PayrollLedger")

        rust = {s.source_name: s for s in self._contract(Language.RUST).symbols}
        self.assertEqual(rust["calculate_net_pay"].target_name, "calculate_net_pay")

    def test_symbols_carry_their_declaring_file(self) -> None:
        by_source = {s.source_name: s for s in self._contract().symbols}
        self.assertEqual(by_source["calculate_net_pay"].source_path, "payroll.py")
        self.assertEqual(by_source["render_report"].source_path, "reports.py")

    def test_extraction_is_deterministic(self) -> None:
        self.assertEqual(self._contract().to_dict(), self._contract().to_dict())

    def test_non_python_sources_are_scanned_by_line(self) -> None:
        files = [
            SourceFile(
                path="main.go",
                language=Language.GO,
                content="package main\n\nfunc computeTotal(a int) int {\n\treturn a\n}\n",
            )
        ]
        contract = stub_extract_contract(files, Language.GO, Language.PYTHON)
        by_source = {s.source_name: s for s in contract.symbols}
        self.assertIn("computeTotal", by_source)
        self.assertEqual(by_source["computeTotal"].target_name, "compute_total")

    def test_cobol_program_ids_are_found(self) -> None:
        files = [
            SourceFile(
                path="pay.cob",
                language=Language.COBOL,
                content="       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PAY-RUN.\n",
            )
        ]
        contract = stub_extract_contract(files, Language.COBOL, Language.PYTHON)
        self.assertEqual(
            [s.target_name for s in contract.symbols], ["pay_run"]
        )

    def test_unparseable_python_falls_back_to_the_line_scan(self) -> None:
        files = [
            SourceFile(
                path="broken.py",
                language=Language.PYTHON,
                content="def still_found(:\n    pass\n",
            )
        ]
        contract = stub_extract_contract(files, Language.PYTHON, Language.GO)
        self.assertEqual(
            [s.source_name for s in contract.symbols], ["still_found"]
        )

    def test_empty_input_yields_an_empty_contract(self) -> None:
        contract = stub_extract_contract([], Language.PYTHON, Language.GO)
        self.assertEqual(len(contract), 0)


class ContractRenderTests(unittest.TestCase):
    def test_the_focus_file_leads(self) -> None:
        contract = stub_extract_contract(_files(), Language.PYTHON, Language.GO)
        rendered = contract.render(focus_path="reports.py")
        self.assertIn("Symbols declared in reports.py:", rendered)
        self.assertLess(
            rendered.index("renderReport"), rendered.index("calculateNetPay")
        )

    def test_rendering_is_truncated_with_a_marker(self) -> None:
        symbols = tuple(
            ContractSymbol(source_name=f"s{i}", target_name=f"t{i}", source_path="a.py")
            for i in range(30)
        )
        contract = Contract(
            source_language=Language.PYTHON,
            target_language=Language.GO,
            symbols=symbols,
        )
        rendered = contract.render(limit=5)
        self.assertIn("and 25 more", rendered)

    def test_an_empty_contract_renders_to_nothing(self) -> None:
        contract = Contract(
            source_language=Language.PYTHON, target_language=Language.GO
        )
        self.assertTrue(contract.is_empty)
        self.assertEqual(contract.render(), "")

    def test_round_trips_through_a_dict(self) -> None:
        contract = stub_extract_contract(_files(), Language.PYTHON, Language.GO)
        self.assertEqual(
            Contract.from_dict(contract.to_dict()).to_dict(), contract.to_dict()
        )


class _Recorder:
    """Captures the context each seam call actually received."""

    def __init__(self) -> None:
        self.translate_contracts: list[Contract | None] = []
        self.translate_paths: list[str] = []
        self.merge_contracts: list[Contract | None] = []
        self.merge_paths: list[str] = []

    def translate(
        self, unit: TranslationUnit, agent: SwarmAgent
    ) -> TranslationResult:
        self.translate_contracts.append(unit.contract)
        self.translate_paths.append(unit.source_path)
        return TranslationResult(
            unit_id=unit.id,
            target_language=unit.target_language,
            translated_content=f"chapter {unit.index}",
            agent_id=agent.id,
            tokens_used=1,
        )

    def merge(self, task: MergeTask, agent: SwarmAgent) -> MergeResult:
        self.merge_contracts.append(task.contract)
        self.merge_paths.append(task.source_path)
        return MergeResult(
            source_file_id=task.source_file_id,
            target_language=task.target_language,
            merged=f"{task.left}\n{task.right}",
            agent_id=agent.id,
            tokens_used=1,
        )


class OrchestratorContractPathTests(unittest.TestCase):
    def _run(self, *, with_contract: bool) -> tuple[_Recorder, list[JobStatus]]:
        recorder = _Recorder()
        saved: list[JobStatus] = []

        class Spy:
            def save(self, job: TranslationJob) -> None:
                saved.append(job.status)

        orchestrator = Orchestrator(
            _agents(2),
            recorder.translate,
            merge_fn=recorder.merge,
            extract_contract_fn=stub_extract_contract if with_contract else None,
            chunker=Chunker(max_lines_per_unit=3),
            persister=Spy(),
        )
        self.report = orchestrator.run(_job())
        return recorder, saved

    def test_lifecycle_routes_through_analyzing(self) -> None:
        _, saved = self._run(with_contract=True)
        self.assertIn(JobStatus.ANALYZING, saved)
        self.assertLess(
            saved.index(JobStatus.ANALYZING), saved.index(JobStatus.DISPATCHED)
        )

    def test_every_translate_call_receives_the_same_contract(self) -> None:
        recorder, _ = self._run(with_contract=True)
        self.assertTrue(recorder.translate_contracts)
        first = recorder.translate_contracts[0]
        self.assertIsNotNone(first)
        assert first is not None
        self.assertGreater(len(first), 0)
        # Identity, not just equality: one table, shared by every agent.
        for contract in recorder.translate_contracts:
            self.assertIs(contract, first)

    def test_merge_calls_receive_the_contract_and_the_file_path(self) -> None:
        recorder, _ = self._run(with_contract=True)
        self.assertTrue(recorder.merge_contracts)
        self.assertTrue(all(c is not None for c in recorder.merge_contracts))
        self.assertTrue(all(p.endswith(".py") for p in recorder.merge_paths))

    def test_units_know_which_file_they_came_from(self) -> None:
        recorder, _ = self._run(with_contract=True)
        self.assertEqual(
            set(recorder.translate_paths), {"payroll.py", "reports.py"}
        )

    def test_report_counts_the_contract(self) -> None:
        self._run(with_contract=True)
        self.assertEqual(self.report.contract_symbols, 3)
        self.assertTrue(self.report.succeeded)

    def test_the_naive_path_is_untouched_when_disabled(self) -> None:
        recorder, saved = self._run(with_contract=False)
        self.assertNotIn(JobStatus.ANALYZING, saved)
        self.assertTrue(all(c is None for c in recorder.translate_contracts))
        self.assertTrue(all(c is None for c in recorder.merge_contracts))
        self.assertEqual(self.report.contract_symbols, 0)
        self.assertTrue(self.report.succeeded)

    def test_a_failing_extractor_fails_the_job_loudly(self) -> None:
        def exploding(
            files: Sequence[SourceFile], source: Language, target: Language
        ) -> Contract:
            raise ValueError("contract pass exploded")

        orchestrator = Orchestrator(
            _agents(1),
            _Recorder().translate,
            extract_contract_fn=exploding,
            chunker=Chunker(max_lines_per_unit=3),
        )
        job = _job()
        with self.assertRaises(ValueError):
            orchestrator.run(job)
        # Even an unexpected seam failure must leave the job terminal, never
        # stranded in ANALYZING.
        self.assertEqual(job.status, JobStatus.FAILED)


class ContractInPromptTests(unittest.TestCase):
    """The contract has to reach the *prompt*, not just the seam."""

    class _CapturingClient:
        def __init__(self) -> None:
            self.user = ""

        def complete(
            self, *, system: str, user: str, model: str | None = None
        ) -> Completion:
            self.user = user
            return Completion(text="func translated() {}", tokens=5)

    def test_translate_prompt_carries_the_path_and_the_contract(self) -> None:
        client = self._CapturingClient()
        contract = stub_extract_contract(_files(), Language.PYTHON, Language.GO)
        unit = TranslationUnit(
            job_id="j",
            source_file_id="f",
            index=0,
            content="def calculate_net_pay(gross, tax):\n    return gross - tax\n",
            source_language=Language.PYTHON,
            target_language=Language.GO,
            source_path="payroll.py",
            contract=contract,
        )
        build_translate_fn(client)(unit, SwarmAgent(name="a"))

        self.assertIn("payroll.py", client.user)
        self.assertIn("calculateNetPay", client.user)
        self.assertIn("Shared contract", client.user)

    def test_a_unit_without_a_contract_still_translates(self) -> None:
        client = self._CapturingClient()
        unit = TranslationUnit(
            job_id="j",
            source_file_id="f",
            index=0,
            content="def f():\n    pass\n",
            source_language=Language.PYTHON,
            target_language=Language.GO,
        )
        result = build_translate_fn(client)(unit, SwarmAgent(name="a"))
        self.assertTrue(result.success)
        self.assertNotIn("Shared contract", client.user)


class GroqExtractionTests(unittest.TestCase):
    class _JsonClient:
        def __init__(self, payload: str) -> None:
            self.payload = payload
            self.calls = 0

        def complete(
            self, *, system: str, user: str, model: str | None = None
        ) -> Completion:
            self.calls += 1
            return Completion(text=self.payload, tokens=10)

    def test_parses_a_well_formed_contract(self) -> None:
        payload = json.dumps(
            {
                "conventions": ["Name functions in camelCase."],
                "symbols": [
                    {
                        "source_name": "calculate_net_pay",
                        "target_name": "calculateNetPay",
                        "kind": "function",
                        "signature": "func calculateNetPay(gross, tax float64) float64",
                        "source_path": "payroll.py",
                    }
                ],
            }
        )
        extract = build_extract_contract_fn(self._JsonClient(payload))
        contract = extract(_files(), Language.PYTHON, Language.GO)

        self.assertEqual(len(contract), 1)
        self.assertEqual(contract.symbols[0].target_name, "calculateNetPay")
        self.assertEqual(contract.conventions, ("Name functions in camelCase.",))

    def test_code_fences_are_tolerated(self) -> None:
        payload = '```json\n{"symbols": [{"source_name": "a", "target_name": "A"}]}\n```'
        extract = build_extract_contract_fn(self._JsonClient(payload))
        contract = extract(_files(), Language.PYTHON, Language.GO)
        self.assertEqual(len(contract), 1)

    def test_junk_entries_are_dropped_not_fatal(self) -> None:
        payload = json.dumps(
            {
                "symbols": [
                    {"source_name": "", "target_name": "X"},
                    {"source_name": "keep", "target_name": "Keep"},
                    {"source_name": "keep", "target_name": "Duplicate"},
                    "not an object",
                ]
            }
        )
        extract = build_extract_contract_fn(self._JsonClient(payload))
        contract = extract(_files(), Language.PYTHON, Language.GO)
        self.assertEqual(
            [(s.source_name, s.target_name) for s in contract.symbols],
            [("keep", "Keep")],
        )

    def test_a_broken_response_degrades_to_the_stub(self) -> None:
        extract = build_extract_contract_fn(self._JsonClient("not json at all"))
        with self.assertLogs("polyglot.brain", level="WARNING"):
            contract = extract(_files(), Language.PYTHON, Language.GO)
        # The deterministic stub's answer, not an empty contract.
        self.assertEqual(len(contract), 3)

    def test_no_source_files_means_no_call(self) -> None:
        client = self._JsonClient("{}")
        contract = build_extract_contract_fn(client)(
            [], Language.PYTHON, Language.GO
        )
        self.assertEqual(client.calls, 0)
        self.assertTrue(contract.is_empty)


class ContractSettingsTests(unittest.TestCase):
    def test_enabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(load_settings().contract_enabled)

    def test_env_can_disable_it(self) -> None:
        with mock.patch.dict(os.environ, {"POLYGLOT_CONTRACT": "off"}, clear=True):
            self.assertFalse(load_settings().contract_enabled)

    def test_ambiguous_value_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"POLYGLOT_CONTRACT": "maybe"}, clear=True):
            with self.assertRaises(ConfigError):
                load_settings()


class ServiceWiringTests(unittest.TestCase):
    """The flag has to reach the pipeline, not just the settings object."""

    def _run_through_service(self, *, enabled: bool) -> dict[str, object]:
        import json as _json

        from api.app import build_app
        from api.http import Request
        from db.connection import Database

        settings = dataclasses.replace(load_settings(), contract_enabled=enabled)
        with Database(":memory:") as db:
            db.init_schema()
            app = build_app(db, settings=settings)
            created = _json.loads(
                app.dispatch(
                    Request(
                        method="POST",
                        path="/jobs",
                        headers={"Content-Type": "application/json"},
                        raw_body=_json.dumps(
                            {
                                "name": "port",
                                "source_language": "python",
                                "target_language": "go",
                                "source_files": [
                                    {"path": "payroll.py", "content": _PAYROLL}
                                ],
                            }
                        ).encode(),
                    )
                ).encode()
            )
            run = app.dispatch(
                Request(
                    method="POST",
                    path=f"/jobs/{created['id']}/run",
                    query={"wait": ["1"]},
                )
            )
            body: dict[str, object] = _json.loads(run.encode())
        return body

    def test_enabled_reports_contract_symbols(self) -> None:
        report = self._run_through_service(enabled=True)["report"]
        assert isinstance(report, dict)
        self.assertGreater(report["contract_symbols"], 0)
        self.assertTrue(report["succeeded"])

    def test_disabled_reports_none(self) -> None:
        report = self._run_through_service(enabled=False)["report"]
        assert isinstance(report, dict)
        self.assertEqual(report["contract_symbols"], 0)
        self.assertTrue(report["succeeded"])


if __name__ == "__main__":
    unittest.main()
