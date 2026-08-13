"""The application service that drives translation jobs over Track A core.

This is the only Track B module that knows how to assemble Track A's moving
parts (chunker, orchestrator, repositories, swarm agents) into whole use cases:
create a job, fetch it, list jobs, run the pipeline, read results. Controllers
call these methods and translate the outcome into HTTP; they never touch the
repositories or the orchestrator directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from config.settings import Settings
from core.chunker import Chunker
from core.merger import MergeFn
from core.orchestrator import Orchestrator, RunReport, TranslateFn
from core.verifier import RepairFn, VerifyFn
from db.connection import Database
from db.repository import JobRepository, OutputRepository, ResultRepository
from models.agent import SwarmAgent
from models.enums import JobStatus, Language
from models.job import TranslationJob
from models.output import AssembledOutput, JobOutput, RunSummary
from models.result import TranslationResult
from models.source import SourceFile
from services.ingest import ingest_source
from services.stub_brain import stub_translate
from services.stub_merger import stub_merge
from services.verification import default_verify


def _derive_name(kind: str, location: str) -> str:
    """A readable default job name from the source location."""
    tail = location.rstrip("/").split("/")[-1] or location
    tail = tail[:-4] if tail.endswith(".git") else tail
    return f"{tail} ({kind})"


class TranslationService:
    """Use-case layer over Track A: create, inspect, and run translation jobs."""

    def __init__(
        self,
        db: Database,
        *,
        settings: Settings,
        translate_fn: TranslateFn | None = None,
        merge_fn: MergeFn | None = None,
        verify_fn: VerifyFn | None = None,
        repair_fn: RepairFn | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = JobRepository(db)
        self._results = ResultRepository(db)
        self._outputs = OutputRepository(db)
        # The Groq Brain plugs in here; default to the offline stub.
        self._translate_fn: TranslateFn = translate_fn or stub_translate
        # The reconciliation Brain plugs in here; default to the offline stub so
        # adjacent chapters are merged pairwise instead of naively concatenated.
        self._merge_fn: MergeFn = merge_fn or stub_merge
        # The verification gate over merged output; the default parses the
        # result offline. A repair seam is opt-in (None => the gate is pass/fail).
        self._verify_fn: VerifyFn = verify_fn or default_verify
        self._repair_fn: RepairFn | None = repair_fn

    # --- Commands -----------------------------------------------------------

    def create_job(
        self,
        *,
        name: str,
        source_language: str,
        target_language: str,
        source_files: Sequence[dict[str, Any]],
    ) -> TranslationJob:
        """Build, persist, and return a new PENDING job.

        Raises ``ValueError`` for unknown languages or invalid field values,
        which the controller surfaces as ``400 Bad Request``.
        """
        source = Language.from_value(source_language)
        target = Language.from_value(target_language)
        files = [
            SourceFile(
                path=str(entry["path"]),
                language=source,
                content=str(entry["content"]),
            )
            for entry in source_files
        ]
        job = TranslationJob(
            name=name,
            source_language=source,
            target_language=target,
            source_files=files,
        )
        self._jobs.save(job)
        return job

    def create_job_from_source(
        self,
        *,
        name: str | None,
        source_language: str,
        target_language: str,
        source_kind: str,
        location: str,
    ) -> TranslationJob:
        """Ingest a local folder or GitHub repo, then create a PENDING job.

        Raises ``ValueError`` (mapped to ``400``) for unknown languages, a bad
        location, or when no matching source files are found.
        """
        source = Language.from_value(source_language)
        files = ingest_source(
            kind=source_kind, location=location, source_language=source
        )
        job_name = (name or "").strip() or _derive_name(source_kind, location)
        return self.create_job(
            name=job_name,
            source_language=source_language,
            target_language=target_language,
            source_files=files,
        )

    def run(self, job: TranslationJob) -> RunReport:
        """Run the full translate pipeline for ``job`` and persist results.

        The orchestrator checkpoints job state to the repository as it advances;
        we additionally persist each per-unit result and, once the run lands,
        the assembled files plus their merge/verify summary — so the outcome
        survives the process and can be re-read by ``GET /jobs/{id}/output``.
        Illegal starting states raise ``OrchestrationError`` (mapped to ``409``
        by the API).
        """
        orchestrator = Orchestrator(
            self._build_agents(),
            self._translate_fn,
            merge_fn=self._merge_fn,
            verify_fn=self._verify_fn,
            repair_fn=self._repair_fn,
            max_repair_attempts=self._settings.max_repair_attempts,
            max_concurrency=self._settings.max_concurrency,
            chunker=self._build_chunker(),
            persister=self._jobs,
        )
        report = orchestrator.run(job)
        for unit in job.units:
            result = report.results.get(unit.id)
            if result is not None:
                self._results.save(result)
        self._save_output(job, report)
        return report

    def record_failure(self, job: TranslationJob, error: str) -> None:
        """Persist why a run aborted, so a poller can read the reason back.

        The orchestrator already moved the job to ``FAILED``; this stores the
        message alongside it (an empty output row) instead of losing it with
        the thread that raised.
        """
        self._outputs.save(
            RunSummary(
                job_id=job.id,
                status=job.status,
                succeeded=False,
                verified=False,
                error=error,
            )
        )

    # --- Queries ------------------------------------------------------------

    def output_for(self, job_id: str) -> JobOutput | None:
        """The stored result of the job's last run, or ``None`` if never run."""
        return self._outputs.get(job_id)

    def get_job(self, job_id: str) -> TranslationJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[TranslationJob]:
        jobs: list[TranslationJob] = []
        for job_id in self._jobs.list_ids():
            job = self._jobs.get(job_id)
            if job is not None:
                jobs.append(job)
        return jobs

    def results_for(self, job: TranslationJob) -> list[TranslationResult]:
        """Stored per-unit results for a job, in unit order (missing skipped)."""
        results: list[TranslationResult] = []
        for unit in job.units:
            result = self._results.get(unit.id)
            if result is not None:
                results.append(result)
        return results

    def agents(self) -> list[SwarmAgent]:
        """The swarm as configured by settings (freshly built, all idle)."""
        return self._build_agents()

    # --- Internals ----------------------------------------------------------

    def _save_output(self, job: TranslationJob, report: RunReport) -> None:
        self._outputs.save(
            RunSummary(
                job_id=job.id,
                status=job.status,
                succeeded=report.succeeded,
                total_tokens=report.total_tokens,
                merges=report.merge_count,
                merge_tokens=report.merge_tokens,
                merge_depth=report.merge_depth,
                verified=report.verified,
                repairs=report.repairs,
            ),
            [
                AssembledOutput(
                    source_file_id=f.source_file_id,
                    source_path=f.source_path,
                    target_language=f.target_language,
                    content=f.content,
                    unit_count=f.unit_count,
                )
                for f in report.assembled_files
            ],
        )

    def _build_agents(self) -> list[SwarmAgent]:
        return [
            SwarmAgent(name=f"groq-worker-{i}", model=self._settings.groq.model)
            for i in range(self._settings.agent_count)
        ]

    def _build_chunker(self) -> Chunker:
        return Chunker(
            max_lines_per_unit=self._settings.max_lines_per_unit,
            overlap_lines=self._settings.overlap_lines,
        )
