"""Contracts for the *reconciliation* seam: merging two translated chapters.

Independent chunks are translated in parallel, so two adjacent chapters can
disagree on names, signatures, or imports where they meet. Rather than blindly
concatenating them, the swarm hands each *pair* of neighbouring pieces to an
agent that reconciles them into one coherent piece, then repeats the process up
a binary tree until a single merged file remains.

``MergeTask`` is what that agent receives; ``MergeResult`` is what it returns.
This mirrors the translate seam (:mod:`models.result`): everything here is a
plain data contract, and the actual intelligence (a second Groq call) plugs in
behind ``merge_fn`` — a deterministic stub stands in for tests and demos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.contract import Contract
from models.enums import Language


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MergeTask:
    """Two adjacent translated pieces to reconcile into one.

    ``left`` precedes ``right`` in source order; a merge must never reorder
    them, only reconcile the seam between them. ``left_span``/``right_span`` are
    the inclusive unit-index ranges each side already covers, and ``depth`` is
    the level in the merge tree (0 = merging two original leaf chapters), both
    useful context for a real Brain prompt.

    ``source_path`` and ``contract`` carry the same job-wide context the
    translate seam receives: a reconciler that knows the agreed name for a
    symbol can unify a seam *towards the contract* instead of picking one
    side's invention.
    """

    source_file_id: str
    target_language: Language
    left: str
    right: str
    left_span: tuple[int, int]
    right_span: tuple[int, int]
    depth: int = 0
    source_path: str = ""
    contract: Contract | None = None

    @property
    def span(self) -> tuple[int, int]:
        """The combined index range the merged output will cover."""
        return (self.left_span[0], self.right_span[1])


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Immutable record of one reconciliation step.

    ``success=False`` carries ``error`` and an empty ``merged``; the orchestrator
    refuses to build a file from a failed merge rather than emit corrupt output.
    """

    source_file_id: str
    target_language: Language
    merged: str
    agent_id: str | None = None
    tokens_used: int = 0
    duration_ms: int = 0
    success: bool = True
    error: str | None = None
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.tokens_used < 0:
            raise ValueError("tokens_used must be >= 0")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        if self.success and self.error:
            raise ValueError("a successful merge must not carry an error")
        if not self.success and not self.error:
            raise ValueError("a failed merge must carry an error message")

    @classmethod
    def failure(
        cls,
        source_file_id: str,
        target_language: Language,
        error: str,
        *,
        agent_id: str | None = None,
    ) -> "MergeResult":
        """Convenience constructor for a failed reconciliation."""
        return cls(
            source_file_id=source_file_id,
            target_language=target_language,
            merged="",
            agent_id=agent_id,
            success=False,
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "target_language": self.target_language.value,
            "merged": self.merged,
            "agent_id": self.agent_id,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }
