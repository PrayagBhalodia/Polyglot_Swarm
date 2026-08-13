"""Track A data contracts for the Polyglot Swarm.

These types are the *contract* every other track consumes. Keep them strict,
serialisable, and free of side effects. Nothing in this package may import from
``core`` or ``db`` -- the dependency arrow points inward, toward the models.
"""

from __future__ import annotations

from models.agent import AgentAssignment, SwarmAgent
from models.enums import AgentStatus, JobStatus, Language, UnitStatus
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit

__all__ = [
    "AgentAssignment",
    "AgentStatus",
    "JobStatus",
    "Language",
    "SourceFile",
    "SwarmAgent",
    "TranslationJob",
    "TranslationResult",
    "TranslationUnit",
    "UnitStatus",
]
