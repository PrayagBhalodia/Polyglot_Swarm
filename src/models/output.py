"""The *persisted* outcome of a pipeline run.

:class:`~core.orchestrator.RunReport` is the in-memory result of one call to
``Orchestrator.run``; it dies with the process. These contracts are its durable
counterpart, written once a run finishes so ``GET /jobs/{id}/output`` can serve
the translated files (and the merge/verify statistics that explain them) long
after the run — which is what makes an *asynchronous* run useful at all.

They deliberately mirror ``RunReport``/``AssembledFile`` rather than reuse them:
``models`` is the contract layer the DB maps rows to, and it must not depend on
``core``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.enums import JobStatus, Language


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class AssembledOutput:
    """One translated file as it was assembled, ready to hand back or download."""

    source_file_id: str
    source_path: str
    target_language: Language
    content: str
    unit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "source_path": self.source_path,
            "target_language": self.target_language.value,
            "unit_count": self.unit_count,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Headline statistics for one run: what it cost and whether it held up."""

    job_id: str
    status: JobStatus
    succeeded: bool = False
    total_tokens: int = 0
    merges: int = 0
    merge_tokens: int = 0
    merge_depth: int = 0
    verified: bool = True
    repairs: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "succeeded": self.succeeded,
            "total_tokens": self.total_tokens,
            "merges": self.merges,
            "merge_tokens": self.merge_tokens,
            "merge_depth": self.merge_depth,
            "verified": self.verified,
            "repairs": self.repairs,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class JobOutput:
    """A run summary together with every file the run produced."""

    summary: RunSummary
    files: tuple[AssembledOutput, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary.to_dict(),
            "assembled_files": [f.to_dict() for f in self.files],
            "file_count": len(self.files),
        }
