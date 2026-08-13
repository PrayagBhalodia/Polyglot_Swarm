"""Contracts describing swarm workers and their unit assignments."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.enums import AgentStatus


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SwarmAgent:
    """A single translator in the swarm.

    Track A models the agent as pure state; the *Brain* track supplies the
    actual Groq-backed translation behaviour through the orchestrator's
    ``translate_fn`` seam. ``model`` names the Groq model the worker will use.
    """

    name: str
    model: str = "llama-3.3-70b-versatile"
    status: AgentStatus = AgentStatus.IDLE
    current_unit_id: str | None = None
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("SwarmAgent.name must be non-empty")
        if not self.model.strip():
            raise ValueError("SwarmAgent.model must be non-empty")

    @property
    def is_available(self) -> bool:
        return self.status == AgentStatus.IDLE and self.current_unit_id is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "status": self.status.value,
            "current_unit_id": self.current_unit_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SwarmAgent":
        return cls(
            id=data["id"],
            name=data["name"],
            model=data.get("model", "llama-3.3-70b-versatile"),
            status=AgentStatus(data.get("status", AgentStatus.IDLE.value)),
            current_unit_id=data.get("current_unit_id"),
        )


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    """An immutable record that agent ``agent_id`` owns unit ``unit_id``."""

    agent_id: str
    unit_id: str
    assigned_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "unit_id": self.unit_id,
            "assigned_at": self.assigned_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentAssignment":
        return cls(
            agent_id=data["agent_id"],
            unit_id=data["unit_id"],
            assigned_at=datetime.fromisoformat(data["assigned_at"]),
        )
