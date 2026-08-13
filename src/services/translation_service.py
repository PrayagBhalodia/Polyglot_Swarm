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
from core.orchestrator import Orchestrator, RunReport, TranslateFn
from db.connection import Database
from db.repository import JobRepository, ResultRepository
from models.agent import SwarmAgent
from models.enums import Language
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile
from services.stub_brain import stub_translate


class TranslationService:
    """Use-case layer over Track A: create, inspect, and run translation jobs."""

    def __init__(
        self,
        db: Database,
        *,
        settings: Settings,
        translate_fn: TranslateFn | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = JobRepository(db)
        self._results = ResultRepository(db)
        # The Groq Brain plugs in here; default to the offline stub.
        self._translate_fn: TranslateFn = translate_fn or stub_translate

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

    def run(self, job: TranslationJob) -> RunReport:
        """Run the full translate pipeline for ``job`` and persist results.

        The orchestrator checkpoints job state to the repository as it advances;
        we additionally persist each per-unit result. Illegal starting states
        raise ``OrchestrationError`` (mapped to ``409`` by the API).
        """
        orchestrator = Orchestrator(
            self._build_agents(),
            self._translate_fn,
            chunker=self._build_chunker(),
            persister=self._jobs,
        )
        report = orchestrator.run(job)
        for unit in job.units:
            result = report.results.get(unit.id)
            if result is not None:
                self._results.save(result)
        return report

    # --- Queries ------------------------------------------------------------

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
