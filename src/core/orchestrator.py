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
from core.errors import OrchestrationError
from models.agent import AgentAssignment, SwarmAgent
from models.enums import AgentStatus, JobStatus, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import TranslationUnit

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
    JobStatus.TRANSLATING: frozenset({JobStatus.ASSEMBLING, JobStatus.FAILED}),
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

    @property
    def succeeded(self) -> bool:
        return self.job.status == JobStatus.COMPLETED

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens_used for r in self.results.values())


class Orchestrator:
    """Coordinates a swarm of agents over a single :class:`TranslationJob`."""

    def __init__(
        self,
        agents: Sequence[SwarmAgent],
        translate_fn: TranslateFn,
        *,
        chunker: Chunker | None = None,
        persister: JobPersister | None = None,
    ) -> None:
        if not agents:
            raise OrchestrationError("orchestrator requires at least one agent")
        self._agents = list(agents)
        self._translate_fn = translate_fn
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
            self._assemble(job, report)
        except OrchestrationError:
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

    def _assemble(self, job: TranslationJob, report: RunReport) -> None:
        if job.failed_units:
            raise OrchestrationError(
                f"{job.failed_units} unit(s) failed to translate; "
                "refusing to assemble a partial result"
            )
        self._transition(job, JobStatus.ASSEMBLING)
        report.assembled_files = assemble_job(job, report.results)
        self._transition(job, JobStatus.COMPLETED)

    def _fail(self, job: TranslationJob) -> None:
        if not job.status.is_terminal:
            job.status = JobStatus.FAILED
            job.touch()
            self._checkpoint(job)
