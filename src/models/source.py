"""Source-side data contracts: files and the chapters we cut them into."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from models.contract import Contract
from models.enums import Language, UnitStatus


def _new_id() -> str:
    return uuid.uuid4().hex


def checksum(content: str) -> str:
    """Stable SHA-256 of file/unit content, used for idempotent re-ingestion."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A single legacy source file fed into a job.

    Immutable: once ingested, a file's identity and content never change. If the
    file on disk changes it becomes a *new* ``SourceFile`` (different checksum).
    """

    path: str
    language: Language
    content: str
    id: str = field(default_factory=_new_id)
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("SourceFile.path must be non-empty")
        if not isinstance(self.language, Language):
            raise TypeError("SourceFile.language must be a Language enum member")
        # Derive checksum lazily but store it so DB rows are self-describing.
        if not self.sha256:
            object.__setattr__(self, "sha256", checksum(self.content))

    @property
    def line_count(self) -> int:
        if not self.content:
            return 0
        return self.content.count("\n") + (0 if self.content.endswith("\n") else 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "language": self.language.value,
            "content": self.content,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceFile":
        return cls(
            id=data["id"],
            path=data["path"],
            language=Language.from_value(data["language"]),
            content=data["content"],
            sha256=data.get("sha256", ""),
        )


@dataclass(slots=True)
class TranslationUnit:
    """One chapter of a source file — the atomic unit an agent translates.

    ``index`` fixes the reassembly order; the assembler relies on it being a
    contiguous 0-based sequence per source file.

    The last two fields are *run-time context*, not state: the file this chapter
    came from and the job-wide :class:`~models.contract.Contract` it must
    translate against. They let ``translate_fn`` see more than a naked chunk
    without changing the seam's signature, and they are derived from the job
    rather than owned by the unit — so they are deliberately absent from
    ``to_dict``/``from_dict`` and from the database row.
    """

    job_id: str
    source_file_id: str
    index: int
    content: str
    source_language: Language
    target_language: Language
    start_line: int = 1
    end_line: int = 1
    status: UnitStatus = UnitStatus.PENDING
    assigned_agent_id: str | None = None
    id: str = field(default_factory=_new_id)
    sha256: str = ""
    source_path: str = ""
    contract: Contract | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("TranslationUnit.index must be >= 0")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError(
                f"invalid line span: start_line={self.start_line}, "
                f"end_line={self.end_line}"
            )
        if self.source_language == self.target_language:
            raise ValueError(
                "source and target language must differ "
                f"(both were {self.source_language.value})"
            )
        if not self.sha256:
            self.sha256 = checksum(self.content)

    def with_status(
        self, status: UnitStatus, *, agent_id: str | None = None
    ) -> "TranslationUnit":
        """Return a copy in a new status (keeps the model side-effect free)."""
        return replace(self, status=status, assigned_agent_id=agent_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "source_file_id": self.source_file_id,
            "index": self.index,
            "content": self.content,
            "source_language": self.source_language.value,
            "target_language": self.target_language.value,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "status": self.status.value,
            "assigned_agent_id": self.assigned_agent_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationUnit":
        return cls(
            id=data["id"],
            job_id=data["job_id"],
            source_file_id=data["source_file_id"],
            index=int(data["index"]),
            content=data["content"],
            source_language=Language.from_value(data["source_language"]),
            target_language=Language.from_value(data["target_language"]),
            start_line=int(data.get("start_line", 1)),
            end_line=int(data.get("end_line", 1)),
            status=UnitStatus(data.get("status", UnitStatus.PENDING.value)),
            assigned_agent_id=data.get("assigned_agent_id"),
            sha256=data.get("sha256", ""),
        )
