"""Repositories: the only code that maps models to/from SQLite rows.

``JobRepository`` persists the whole job aggregate (job + source files + units)
transactionally, and satisfies the orchestrator's ``JobPersister`` protocol via
its :meth:`save` method. ``ResultRepository`` stores per-unit translations.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from core.errors import RepositoryError
from db.connection import Database
from models.enums import JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit


class JobRepository:
    """CRUD for the job aggregate (job, its source files, and its units)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, job: TranslationJob) -> None:
        """Upsert the entire aggregate atomically.

        Safe to call repeatedly (it is the orchestrator's checkpoint hook), so
        every write is an UPSERT keyed on primary key.
        """
        try:
            with self._db.transaction() as conn:
                self._upsert_job(conn, job)
                for source_file in job.source_files:
                    self._upsert_source_file(conn, job.id, source_file)
                for unit in job.units:
                    self._upsert_unit(conn, unit)
        except sqlite3.Error as exc:
            raise RepositoryError(f"failed to save job {job.id!r}: {exc}") from exc

    @staticmethod
    def _upsert_job(conn: sqlite3.Connection, job: TranslationJob) -> None:
        conn.execute(
            """
            INSERT INTO jobs (id, name, source_language, target_language,
                              status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                source_language=excluded.source_language,
                target_language=excluded.target_language,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                job.id,
                job.name,
                job.source_language.value,
                job.target_language.value,
                job.status.value,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _upsert_source_file(
        conn: sqlite3.Connection, job_id: str, source_file: SourceFile
    ) -> None:
        conn.execute(
            """
            INSERT INTO source_files (id, job_id, path, language, content, sha256)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                language=excluded.language,
                content=excluded.content,
                sha256=excluded.sha256
            """,
            (
                source_file.id,
                job_id,
                source_file.path,
                source_file.language.value,
                source_file.content,
                source_file.sha256,
            ),
        )

    @staticmethod
    def _upsert_unit(conn: sqlite3.Connection, unit: TranslationUnit) -> None:
        conn.execute(
            """
            INSERT INTO units (id, job_id, source_file_id, idx, content,
                               source_language, target_language, start_line,
                               end_line, status, assigned_agent_id, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                status=excluded.status,
                assigned_agent_id=excluded.assigned_agent_id,
                start_line=excluded.start_line,
                end_line=excluded.end_line,
                sha256=excluded.sha256
            """,
            (
                unit.id,
                unit.job_id,
                unit.source_file_id,
                unit.index,
                unit.content,
                unit.source_language.value,
                unit.target_language.value,
                unit.start_line,
                unit.end_line,
                unit.status.value,
                unit.assigned_agent_id,
                unit.sha256,
            ),
        )

    def get(self, job_id: str) -> TranslationJob | None:
        """Load a full job aggregate by id, or ``None`` if absent."""
        try:
            conn = self._db.connection
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None

            files = [
                self._row_to_source_file(r)
                for r in conn.execute(
                    "SELECT * FROM source_files WHERE job_id = ?", (job_id,)
                ).fetchall()
            ]
            units = [
                self._row_to_unit(r)
                for r in conn.execute(
                    "SELECT * FROM units WHERE job_id = ? ORDER BY idx", (job_id,)
                ).fetchall()
            ]
        except sqlite3.Error as exc:
            raise RepositoryError(f"failed to load job {job_id!r}: {exc}") from exc

        return TranslationJob(
            id=row["id"],
            name=row["name"],
            source_language=Language.from_value(row["source_language"]),
            target_language=Language.from_value(row["target_language"]),
            status=JobStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            source_files=files,
            units=units,
        )

    def list_ids(self) -> list[str]:
        try:
            rows = self._db.connection.execute(
                "SELECT id FROM jobs ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RepositoryError(f"failed to list jobs: {exc}") from exc
        return [r["id"] for r in rows]

    @staticmethod
    def _row_to_source_file(row: sqlite3.Row) -> SourceFile:
        return SourceFile(
            id=row["id"],
            path=row["path"],
            language=Language.from_value(row["language"]),
            content=row["content"],
            sha256=row["sha256"],
        )

    @staticmethod
    def _row_to_unit(row: sqlite3.Row) -> TranslationUnit:
        return TranslationUnit(
            id=row["id"],
            job_id=row["job_id"],
            source_file_id=row["source_file_id"],
            index=row["idx"],
            content=row["content"],
            source_language=Language.from_value(row["source_language"]),
            target_language=Language.from_value(row["target_language"]),
            start_line=row["start_line"],
            end_line=row["end_line"],
            status=UnitStatus(row["status"]),
            assigned_agent_id=row["assigned_agent_id"],
            sha256=row["sha256"],
        )


class ResultRepository:
    """Stores and retrieves per-unit :class:`TranslationResult` rows."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, result: TranslationResult) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO results (unit_id, target_language,
                        translated_content, agent_id, tokens_used, duration_ms,
                        success, error, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(unit_id) DO UPDATE SET
                        translated_content=excluded.translated_content,
                        agent_id=excluded.agent_id,
                        tokens_used=excluded.tokens_used,
                        duration_ms=excluded.duration_ms,
                        success=excluded.success,
                        error=excluded.error,
                        created_at=excluded.created_at
                    """,
                    (
                        result.unit_id,
                        result.target_language.value,
                        result.translated_content,
                        result.agent_id,
                        result.tokens_used,
                        result.duration_ms,
                        1 if result.success else 0,
                        result.error,
                        result.created_at.isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(
                f"failed to save result for unit {result.unit_id!r}: {exc}"
            ) from exc

    def get(self, unit_id: str) -> TranslationResult | None:
        try:
            row = self._db.connection.execute(
                "SELECT * FROM results WHERE unit_id = ?", (unit_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise RepositoryError(
                f"failed to load result for unit {unit_id!r}: {exc}"
            ) from exc
        if row is None:
            return None
        return TranslationResult(
            unit_id=row["unit_id"],
            target_language=Language.from_value(row["target_language"]),
            translated_content=row["translated_content"],
            agent_id=row["agent_id"],
            tokens_used=row["tokens_used"],
            duration_ms=row["duration_ms"],
            success=bool(row["success"]),
            error=row["error"],
            created_at=_parse_dt(row["created_at"]),
        )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
