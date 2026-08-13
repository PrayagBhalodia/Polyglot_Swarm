"""Gate merged output on whether it actually parses, with bounded repair.

A reconciled file is still only as trustworthy as the agents that produced it.
The :class:`Verifier` closes the loop: it runs a ``verify_fn`` check over each
:class:`~core.merger.MergedFile` and, on failure, hands the broken file and its
diagnostics to a ``repair_fn`` agent — re-checking after each fix, up to a bound.
A file that still fails after its repair budget aborts the job rather than
letting broken code reach assembly.

Both the check and the repair are seams:

* ``verify_fn(content, language) -> (ok, errors)`` — a real parser in
  production (``ast.parse`` for Python); a deterministic default ships here.
* ``repair_fn(request, agent) -> fixed_content`` — a Groq-backed fixer in
  production; ``None`` (no repair) is the safe default.

With ``repair_fn`` absent the gate is pass/fail; with it present the gate becomes
the self-correcting step that turns stacked guesses into a verified result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

from core.errors import VerificationError
from core.merger import MergedFile
from models.agent import SwarmAgent
from models.enums import AgentStatus, Language
from models.job import TranslationJob
from models.verification import RepairRequest, VerificationResult

# Seams. ``verify_fn`` reports whether content parses and why not; ``repair_fn``
# returns a fixed version of a failing file.
VerifyFn = Callable[[str, Language], tuple[bool, list[str]]]
RepairFn = Callable[[RepairRequest, SwarmAgent], str]


class Verifier:
    """Verifies (and, if configured, repairs) a job's merged files.

    Parameters
    ----------
    verify_fn:
        The parse/soundness check applied to each merged file.
    repair_fn:
        Optional fixer invoked when verification fails. Requires ``agents``.
    agents:
        Swarm the repair tasks are dispatched across (round-robin).
    max_attempts:
        Upper bound on repair rounds per file before the gate gives up.
    max_concurrency:
        How many files may be checked (and repaired) at once. ``1`` keeps the
        gate strictly sequential.
    """

    def __init__(
        self,
        verify_fn: VerifyFn,
        *,
        repair_fn: RepairFn | None = None,
        agents: Sequence[SwarmAgent] = (),
        max_attempts: int = 1,
        max_concurrency: int = 1,
    ) -> None:
        if max_attempts < 0:
            raise VerificationError("max_attempts must be >= 0")
        if max_concurrency < 1:
            raise VerificationError("max_concurrency must be >= 1")
        if repair_fn is not None and not agents:
            raise VerificationError("repair requires at least one agent")
        self._verify_fn = verify_fn
        self._repair_fn = repair_fn
        self._agents = list(agents)
        self._max_attempts = max_attempts
        self._max_concurrency = max_concurrency
        self._cursor = 0

    def verify(
        self, job: TranslationJob, merged_files: Sequence[MergedFile]
    ) -> tuple[list[MergedFile], list[VerificationResult]]:
        """Check every merged file, repairing failures within budget.

        Returns the (possibly repaired) files alongside a per-file result. The
        caller decides what a failing result means — the orchestrator fails the
        job rather than assemble unverified output.
        """
        # Files are independent, so each one's check-and-repair loop can run in
        # its own thread. The repair agent is picked here, on the calling
        # thread, so the round-robin cursor is never touched concurrently, and
        # results are collected in submission order — the gate is parallel but
        # its report is identical to the sequential one.
        pool = [a for a in self._agents if a.status != AgentStatus.OFFLINE]
        outcomes = self._run_all(
            [
                partial(
                    self._verify_one,
                    mf,
                    pool[(self._cursor + i) % len(pool)] if pool else None,
                    job.target_language,
                )
                for i, mf in enumerate(merged_files)
            ]
        )
        self._cursor += len(merged_files)
        return [outcome[0] for outcome in outcomes], [
            outcome[1] for outcome in outcomes
        ]

    # --- Internals ----------------------------------------------------------

    def _verify_one(
        self, mf: MergedFile, agent: SwarmAgent | None, language: Language
    ) -> tuple[MergedFile, VerificationResult]:
        """Check one file, repairing it within budget; pure w.r.t. shared state."""
        content = mf.content
        ok, errors = self._verify_fn(content, language)

        attempts = 0
        while not ok and self._repair_fn is not None and attempts < self._max_attempts:
            if agent is None:
                raise VerificationError("no online agents available for repair")
            request = RepairRequest(
                source_file_id=mf.source_file_id,
                target_language=language,
                content=content,
                errors=tuple(errors),
            )
            content = self._repair_fn(request, agent)
            ok, errors = self._verify_fn(content, language)
            attempts += 1

        return (
            replace(mf, content=content),
            VerificationResult(
                source_file_id=mf.source_file_id,
                ok=ok,
                errors=tuple(errors),
                attempts=attempts,
            ),
        )

    def _run_all(
        self, calls: Sequence[Callable[[], tuple[MergedFile, VerificationResult]]]
    ) -> list[tuple[MergedFile, VerificationResult]]:
        if len(calls) <= 1 or self._max_concurrency <= 1:
            return [call() for call in calls]
        workers = min(self._max_concurrency, len(calls))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="polyglot-verify"
        ) as executor:
            futures = [executor.submit(call) for call in calls]
            return [future.result() for future in futures]
