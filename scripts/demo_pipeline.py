#!/usr/bin/env python3
"""End-to-end Track A demo with a *stubbed* Brain.

Runs the whole coordination pipeline -- agree the cross-file contract, chunk,
dispatch across a swarm (concurrently), reconcile, verify, reassemble -- against
a tiny inline COBOL snippet, persisting to an in-memory DB. Every seam is faked
locally so this runs with zero network and no API key, demonstrating exactly
where the Groq-powered Brain track plugs in.

Usage:
    python scripts/demo_pipeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config.settings import load_settings  # noqa: E402
from core.chunker import Chunker  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402
from db.connection import Database  # noqa: E402
from db.repository import JobRepository, ResultRepository  # noqa: E402
from models.agent import SwarmAgent  # noqa: E402
from models.enums import Language  # noqa: E402
from models.job import TranslationJob  # noqa: E402
from models.result import TranslationResult  # noqa: E402
from models.source import SourceFile, TranslationUnit  # noqa: E402
from services.stub_contract import stub_extract_contract  # noqa: E402
from services.stub_merger import stub_merge  # noqa: E402
from services.verification import default_verify  # noqa: E402

LEGACY_COBOL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYROLL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-GROSS      PIC 9(6)V99.
       01 WS-TAX        PIC 9(6)V99.
       01 WS-NET        PIC 9(6)V99.
       PROCEDURE DIVISION.
           COMPUTE WS-TAX = WS-GROSS * 0.2.
           COMPUTE WS-NET = WS-GROSS - WS-TAX.
           DISPLAY "NET PAY: " WS-NET.
           STOP RUN.
"""


def stub_translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
    """Stand-in for the Groq Brain: emits a labelled Python placeholder."""
    comments = [f"    # {line.strip()}" for line in unit.content.splitlines() if line.strip()]
    # End the body with a real statement so the placeholder is valid Python.
    body = "\n".join([*comments, "    pass"])
    translated = (
        f"# --- chapter {unit.index} (translated by {agent.name}) ---\n"
        f"def chapter_{unit.index}():\n{body}\n"
    )
    return TranslationResult(
        unit_id=unit.id,
        target_language=unit.target_language,
        translated_content=translated,
        agent_id=agent.id,
        tokens_used=len(unit.content),
        duration_ms=1,
    )


def main() -> int:
    settings = load_settings()
    print(f"Loaded settings: {settings.agent_count} agents "
          f"(max {settings.max_concurrency} in flight), "
          f"target={settings.default_target_language.value}, "
          f"model={settings.groq.model}")

    db = Database(":memory:")
    db.init_schema()
    job_repo = JobRepository(db)
    result_repo = ResultRepository(db)

    job = TranslationJob(
        name="legacy-payroll",
        source_language=Language.COBOL,
        target_language=Language.PYTHON,
        source_files=[
            SourceFile(
                path="payroll.cob", language=Language.COBOL, content=LEGACY_COBOL
            )
        ],
    )

    agents = [SwarmAgent(name=f"groq-worker-{i}") for i in range(settings.agent_count)]
    orch = Orchestrator(
        agents,
        stub_translate,
        merge_fn=stub_merge,
        verify_fn=default_verify,
        extract_contract_fn=stub_extract_contract,
        max_concurrency=settings.max_concurrency,
        chunker=Chunker(
            max_lines_per_unit=4, strategy=settings.chunk_strategy
        ),
        persister=job_repo,
    )

    report = orch.run(job)

    for unit in job.units:
        result_repo.save(report.results[unit.id])

    print(f"\nJob {job.name!r} -> {job.status.value}")
    print(f"Contract: {report.contract_symbols} shared symbol(s) agreed "
          f"before any chapter was translated")
    print(f"Units: {job.total_units}  |  progress: {job.progress:.0%}  |  "
          f"tokens: {report.total_tokens}")
    print(f"Merge tree: {report.merge_count} reconciliation(s), "
          f"depth {report.merge_depth}, merge tokens {report.merge_tokens}")
    print(f"Verification: {'passed' if report.verified else 'FAILED'} "
          f"({len(report.verifications)} file(s), {report.repairs} repair(s))")
    print("\n===== ASSEMBLED PYTHON =====\n")
    for assembled in report.assembled_files:
        print(f"# file: {assembled.source_path}  ({assembled.unit_count} chapters)")
        print(assembled.content)

    db.close()
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
