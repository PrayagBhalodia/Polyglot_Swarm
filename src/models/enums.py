"""Enumerations shared across the Polyglot Swarm data contracts.

All enums subclass ``str`` so they serialise cleanly to JSON and persist as
plain text columns in SQLite without custom adapters.
"""

from __future__ import annotations

from enum import Enum


class Language(str, Enum):
    """Programming languages the swarm can read (source) or emit (target)."""

    # --- Legacy / source languages ---
    COBOL = "cobol"
    FORTRAN = "fortran"
    PERL = "perl"
    VB6 = "vb6"
    DELPHI = "delphi"
    JAVA = "java"

    # --- Modern / target languages ---
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"

    @classmethod
    def from_value(cls, value: str) -> "Language":
        """Parse a language from a case-insensitive string.

        Raises ``ValueError`` with the list of accepted values so callers get an
        actionable message instead of a bare ``KeyError``.
        """
        normalised = value.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        accepted = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown language {value!r}; expected one of: {accepted}")


class JobStatus(str, Enum):
    """Lifecycle of a whole :class:`~models.job.TranslationJob`.

    The orchestrator advances a job through these states in order; ``FAILED`` is
    terminal and reachable from any active state.
    """

    PENDING = "pending"        # created, nothing chunked yet
    CHUNKING = "chunking"      # splitting source files into units
    DISPATCHED = "dispatched"  # units assigned to agents
    TRANSLATING = "translating"  # agents are working
    MERGING = "merging"        # agents reconcile adjacent chapters pairwise
    ASSEMBLING = "assembling"  # taping merged chapters back together
    COMPLETED = "completed"    # terminal success
    FAILED = "failed"          # terminal failure

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED)


class UnitStatus(str, Enum):
    """Lifecycle of a single :class:`~models.source.TranslationUnit` (a chapter)."""

    PENDING = "pending"        # waiting to be picked up
    ASSIGNED = "assigned"      # handed to an agent
    TRANSLATING = "translating"
    TRANSLATED = "translated"  # terminal success
    FAILED = "failed"          # terminal failure

    @property
    def is_terminal(self) -> bool:
        return self in (UnitStatus.TRANSLATED, UnitStatus.FAILED)


class AgentStatus(str, Enum):
    """Availability of a swarm worker."""

    IDLE = "idle"        # ready to accept a unit
    BUSY = "busy"        # currently translating
    OFFLINE = "offline"  # not participating in dispatch
