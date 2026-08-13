"""The Orchestrator: drives one job from raw legacy code to assembled output.

This is the "manager in the room". It does **no** translation itself. Instead it
owns the *coordination contract*:

1. chunk the source files into units (the scissors),
2. hand each unit to an available agent (dispatch),
3. record the result and advance unit/agent state,
4. reassemble the translated chapters,
5. advance the job through a validated lifecycle.

The actual intelligence -- the Groq call that turns COBOL into Python -- is
injected as ``translate_fn``. Track A defines *when* and *in what order* work
happens; the Brain track defines *how* a single unit is translated. That seam is
what lets this whole module run, and be tested, with zero network access.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from core.assembler import AssembledFile, assemble_job
from core.chunker import Chunker
from core.errors import PolyglotSwarmError, OrchestrationError, VerificationError
from core.merger import MergedFile, MergeFn, Merger, assemble_merged
from core.verifier import RepairFn, Verifier, VerifyFn
from models.agent import AgentAssignment, SwarmAgent
from models.enums import AgentStatus, JobStatus, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import TranslationUnit
from models.verification import VerificationResult

# The seam the Brain track plugs into: translate one unit using one agent.
TranslateFn = Callable[[TranslationUnit, SwarmAgent], TranslationResult]


class JobPersister(Protocol):
    """Minimal structural type so the orchestrator can checkpoint progress.

    The DB layer's ``JobRepository`` satisfies this without an explicit import,
    keeping ``core`` independent of ``db``.
    """

    def save(self, job: TranslationJob) -> None: ...


# Which job states may legally follow which. ``FAILED`` is reachable from any
# active (non-terminal) state and is enforced separately.
_LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.CHUNKING, JobStatus.FAILED}),
    JobStatus.CHUNKING: frozenset({JobStatus.DISPATCHED, JobStatus.FAILED}),
    JobStatus.DISPATCHED: frozenset({JobStatus.TRANSLATING, JobStatus.FAILED}),
    # TRANSLATING may go straight to ASSEMBLING (naive join) or route through
    # MERGING first when agent-based reconciliation is enabled.
    JobStatus.TRANSLATING: frozenset(
        {JobStatus.MERGING, JobStatus.ASSEMBLING, JobStatus.FAILED}
    ),
    # MERGING may go straight to ASSEMBLING or through the VERIFYING gate first.
    JobStatus.MERGING: frozenset(
        {JobStatus.VERIFYING, JobStatus.ASSEMBLING, JobStatus.FAILED}
    ),
    JobStatus.VERIFYING: frozenset({JobStatus.ASSEMBLING, JobStatus.FAILED}),
    JobStatus.ASSEMBLING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


@dataclass(slots=True)
class RunReport:
    """Outcome of a full orchestration run."""

    job: TranslationJob
    results: dict[str, TranslationResult] = field(default_factory=dict)
    assembled_files: list[AssembledFile] = field(default_factory=list)
    assignments: list[AgentAssignment] = field(default_factory=list)
    merged_files: list[MergedFile] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.job.status == JobStatus.COMPLETED

    @property
    def total_tokens(self) -> int:
        """Tokens spent translating chapters (merge cost is tracked separately)."""
        return sum(r.tokens_used for r in self.results.values())

    @property
    def merge_count(self) -> int:
        """Number of pairwise reconciliation steps performed across all files."""
        return sum(m.merge_count for m in self.merged_files)

    @property
    def merge_tokens(self) -> int:
        return sum(m.tokens_used for m in self.merged_files)

    @property
    def merge_depth(self) -> int:
        """Height of the tallest merge tree (0 when no merging happened)."""
        return max((m.depth for m in self.merged_files), default=0)

    @property
    def verified(self) -> bool:
        """True when every verified file passed (vacuously true if none ran)."""
        return all(v.ok for v in self.verifications)

    @property
    def repairs(self) -> int:
        """Total repair rounds performed across all files during verification."""
        return sum(v.attempts for v in self.verifications)


class Orchestrator:
    """Coordinates a swarm of agents over a single :class:`TranslationJob`."""

    def __init__(
        self,
        agents: Sequence[SwarmAgent],
        translate_fn: TranslateFn,
        *,
        merge_fn: MergeFn | None = None,
        verify_fn: VerifyFn | None = None,
        repair_fn: RepairFn | None = None,
        max_repair_attempts: int = 1,
        chunker: Chunker | None = None,
        persister: JobPersister | None = None,
    ) -> None:
        if not agents:
            raise OrchestrationError("orchestrator requires at least one agent")
        self._agents = list(agents)
        self._translate_fn = translate_fn
        # When a merge seam is supplied, chapters are reconciled pairwise before
        # assembly; otherwise the pipeline falls back to a naive ordered join.
        self._merge_fn = merge_fn
        # An optional gate over merged output: verify each file and, if a repair
        # seam is present, fix failures within budget before assembling.
        self._verify_fn = verify_fn
        self._repair_fn = repair_fn
        self._max_repair_attempts = max_repair_attempts
        self._chunker = chunker or Chunker()
        self._persister = persister

    # --- Lifecycle helpers --------------------------------------------------

    def _transition(self, job: TranslationJob, target: JobStatus) -> None:
        allowed = _LEGAL_TRANSITIONS.get(job.status, frozenset())
        if target not in allowed:
            raise OrchestrationError(
                f"illegal job transition {job.status.value} -> {target.value}"
            )
        job.status = target
        job.touch()
        self._checkpoint(job)

    def _checkpoint(self, job: TranslationJob) -> None:
        if self._persister is not None:
            self._persister.save(job)

    def _next_agent(self, cursor: int) -> SwarmAgent:
        """Round-robin over agents that are participating (not OFFLINE)."""
        pool = [a for a in self._agents if a.status != AgentStatus.OFFLINE]
        if not pool:
            raise OrchestrationError("no online agents available for dispatch")
        return pool[cursor % len(pool)]

    # --- The pipeline -------------------------------------------------------

    def run(self, job: TranslationJob) -> RunReport:
        """Execute the full translate pipeline for ``job`` and report results.

        Raises
        ------
        OrchestrationError
            On an illegal starting state or if no source files are present.
        """
        if job.status != JobStatus.PENDING:
            raise OrchestrationError(
                f"job {job.id!r} must be PENDING to run, was {job.status.value}"
            )
        if not job.source_files:
            raise OrchestrationError(f"job {job.id!r} has no source files")

        report = RunReport(job=job)
        try:
            self._chunk(job)
            self._dispatch(job, report)
            self._translate(job, report)
            self._merge(job, report)
            self._verify(job, report)
            self._assemble(job, report)
        except PolyglotSwarmError:
            self._fail(job)
            raise

        return report

    def _chunk(self, job: TranslationJob) -> None:
        self._transition(job, JobStatus.CHUNKING)
        job.units = self._chunker.chunk_files(
            job.source_files,
            job_id=job.id,
            target_language=job.target_language,
        )
        self._checkpoint(job)

    def _dispatch(self, job: TranslationJob, report: RunReport) -> None:
        self._transition(job, JobStatus.DISPATCHED)
        for cursor, unit in enumerate(job.units):
            agent = self._next_agent(cursor)
            unit.status = UnitStatus.ASSIGNED
            unit.assigned_agent_id = agent.id
            report.assignments.append(
                AgentAssignment(agent_id=agent.id, unit_id=unit.id)
            )
        self._checkpoint(job)

    def _translate(self, job: TranslationJob, report: RunReport) -> None:
        self._transition(job, JobStatus.TRANSLATING)
        agents_by_id = {a.id: a for a in self._agents}

        for unit in job.units:
            agent = agents_by_id.get(unit.assigned_agent_id or "")
            if agent is None:
                raise OrchestrationError(
                    f"unit index {unit.index} has no valid assigned agent"
                )
            agent.status = AgentStatus.BUSY
            agent.current_unit_id = unit.id
            unit.status = UnitStatus.TRANSLATING

            result = self._translate_fn(unit, agent)
            report.results[unit.id] = result
            unit.status = (
                UnitStatus.TRANSLATED if result.success else UnitStatus.FAILED
            )

            agent.status = AgentStatus.IDLE
            agent.current_unit_id = None

        self._checkpoint(job)

    def _merge(self, job: TranslationJob, report: RunReport) -> None:
        """Reconcile adjacent chapters pairwise (skipped in the naive path)."""
        if self._merge_fn is None:
            return
        self._ensure_no_failed_units(job)
        self._transition(job, JobStatus.MERGING)
        merger = Merger(self._merge_fn, self._agents)
        report.merged_files = merger.merge_job(job, report.results)
        self._checkpoint(job)

    def _verify(self, job: TranslationJob, report: RunReport) -> None:
        """Gate merged output on parse-soundness (skipped without a verify seam).

        Only meaningful once chapters have been merged; a failing file that its
        repair budget cannot fix aborts the job rather than assembling broken
        code.
        """
        if self._merge_fn is None or self._verify_fn is None:
            return
        self._transition(job, JobStatus.VERIFYING)
        verifier = Verifier(
            self._verify_fn,
            repair_fn=self._repair_fn,
            agents=self._agents,
            max_attempts=self._max_repair_attempts,
        )
        verified, results = verifier.verify(job, report.merged_files)
        # Adopt the (possibly repaired) content for assembly.
        report.merged_files = verified
        report.verifications = results
        self._checkpoint(job)

        failures = [r for r in results if not r.ok]
        if failures:
            detail = "; ".join(
                f"{r.source_file_id} ({', '.join(r.errors) or 'invalid'})"
                for r in failures
            )
            raise VerificationError(
                f"{len(failures)} merged file(s) failed verification: {detail}"
            )

    def _assemble(self, job: TranslationJob, report: RunReport) -> None:
        if self._merge_fn is None:
            # Naive path: the failed-unit guard runs here since merging was
            # skipped; join the raw translated chapters in order.
            self._ensure_no_failed_units(job)
            self._transition(job, JobStatus.ASSEMBLING)
            report.assembled_files = assemble_job(job, report.results)
        else:
            self._transition(job, JobStatus.ASSEMBLING)
            report.assembled_files = assemble_merged(job, report.merged_files)
        self._transition(job, JobStatus.COMPLETED)

    def _ensure_no_failed_units(self, job: TranslationJob) -> None:
        if job.failed_units:
            raise OrchestrationError(
                f"{job.failed_units} unit(s) failed to translate; "
                "refusing to assemble a partial result"
            )

    def _fail(self, job: TranslationJob) -> None:
        if not job.status.is_terminal:
            job.status = JobStatus.FAILED
            job.touch()
            self._checkpoint(job)
