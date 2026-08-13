"""The top-level aggregate: a translation job and its progress contract."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.enums import JobStatus, Language, UnitStatus
from models.source import SourceFile, TranslationUnit


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TranslationJob:
    """One end-to-end translation of a legacy codebase into a target language.

    The job is the aggregate root: it owns its source files and units. The
    orchestrator mutates ``status`` and the units; everything else is set at
    creation. Progress is *derived*, never stored, so it can never drift out of
    sync with the units.
    """

    name: str
    source_language: Language
    target_language: Language
    status: JobStatus = JobStatus.PENDING
    source_files: list[SourceFile] = field(default_factory=list)
    units: list[TranslationUnit] = field(default_factory=list)
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("TranslationJob.name must be non-empty")
        if self.source_language == self.target_language:
            raise ValueError(
                "source and target language must differ "
                f"(both were {self.source_language.value})"
            )

    # --- Derived progress contract -----------------------------------------

    @property
    def total_units(self) -> int:
        return len(self.units)

    @property
    def completed_units(self) -> int:
        return sum(1 for u in self.units if u.status == UnitStatus.TRANSLATED)

    @property
    def failed_units(self) -> int:
        return sum(1 for u in self.units if u.status == UnitStatus.FAILED)

    @property
    def progress(self) -> float:
        """Fraction of units in a terminal state, in ``[0.0, 1.0]``."""
        if not self.units:
            return 0.0
        terminal = sum(1 for u in self.units if u.status.is_terminal)
        return terminal / len(self.units)

    @property
    def is_done(self) -> bool:
        """True when every unit has reached a terminal state."""
        return bool(self.units) and all(u.status.is_terminal for u in self.units)

    def touch(self) -> None:
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        # The derived counters ride along so a poller watching an asynchronous
        # run can read progress straight off the job it already fetched. They
        # are computed, never stored, so ``from_dict`` simply ignores them.
        return {
            "id": self.id,
            "name": self.name,
            "source_language": self.source_language.value,
            "target_language": self.target_language.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "total_units": self.total_units,
            "completed_units": self.completed_units,
            "failed_units": self.failed_units,
            "progress": self.progress,
            "source_files": [f.to_dict() for f in self.source_files],
            "units": [u.to_dict() for u in self.units],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationJob":
        return cls(
            id=data["id"],
            name=data["name"],
            source_language=Language.from_value(data["source_language"]),
            target_language=Language.from_value(data["target_language"]),
            status=JobStatus(data.get("status", JobStatus.PENDING.value)),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            source_files=[
                SourceFile.from_dict(f) for f in data.get("source_files", [])
            ],
            units=[TranslationUnit.from_dict(u) for u in data.get("units", [])],
        )
