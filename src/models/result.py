"""The output contract: what an agent hands back for one translated unit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.enums import Language


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Immutable record of one unit's translation.

    This is the boundary between the *Brain* track (which produces
    ``translated_content`` via Groq) and Track A (which stores and reassembles
    it). ``success=False`` carries ``error`` and an empty translation.
    """

    unit_id: str
    target_language: Language
    translated_content: str
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
            raise ValueError("a successful result must not carry an error")
        if not self.success and not self.error:
            raise ValueError("a failed result must carry an error message")

    @classmethod
    def failure(
        cls,
        unit_id: str,
        target_language: Language,
        error: str,
        *,
        agent_id: str | None = None,
    ) -> "TranslationResult":
        """Convenience constructor for a failed translation."""
        return cls(
            unit_id=unit_id,
            target_language=target_language,
            translated_content="",
            agent_id=agent_id,
            success=False,
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "target_language": self.target_language.value,
            "translated_content": self.translated_content,
            "agent_id": self.agent_id,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationResult":
        return cls(
            unit_id=data["unit_id"],
            target_language=Language.from_value(data["target_language"]),
            translated_content=data["translated_content"],
            agent_id=data.get("agent_id"),
            tokens_used=int(data.get("tokens_used", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            success=bool(data.get("success", True)),
            error=data.get("error"),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else _now(),
        )
