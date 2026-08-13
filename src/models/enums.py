"""Enumerations shared across the Polyglot Swarm data contracts.

All enums subclass ``str`` so they serialise cleanly to JSON and persist as
plain text columns in SQLite without custom adapters.
"""

from __future__ import annotations

from enum import Enum


class Language(str, Enum):
    """Programming languages the swarm can read *or* emit.

    Any member may be a source or a target — the split below is only a hint for
    the UI's ordering, not a restriction (the sole rule is that a job's source
    and target must differ). Common spellings like ``c++``/``c#``/``js`` are
    accepted by :meth:`from_value` via :data:`_ALIASES`.
    """

    # --- Modern / general-purpose ---
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    GO = "go"
    RUST = "rust"
    RUBY = "ruby"
    PHP = "php"
    SWIFT = "swift"
    KOTLIN = "kotlin"

    # --- Legacy ---
    COBOL = "cobol"
    FORTRAN = "fortran"
    PERL = "perl"
    VB6 = "vb6"
    DELPHI = "delphi"

    @classmethod
    def from_value(cls, value: str) -> "Language":
        """Parse a language from a case-insensitive string (aliases allowed).

        Raises ``ValueError`` with the list of accepted values so callers get an
        actionable message instead of a bare ``KeyError``.
        """
        normalised = value.strip().lower()
        normalised = _ALIASES.get(normalised, normalised)
        for member in cls:
            if member.value == normalised:
                return member
        accepted = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown language {value!r}; expected one of: {accepted}")


# Common alternate spellings mapped to canonical enum values.
_ALIASES: dict[str, str] = {
    "c++": "cpp",
    "cplusplus": "cpp",
    "cxx": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "py": "python",
    "python3": "python",
    "rs": "rust",
    "golang": "go",
    "rb": "ruby",
}


class ChunkStrategy(str, Enum):
    """How the :class:`~core.chunker.Chunker` picks unit boundaries.

    ``STRUCTURAL`` prefers to cut where a top-level construct ends, so a
    function or class lands in one unit; ``LINES`` is the original fixed-size
    split, kept for reproducing older runs and for content with no structure to
    find. Both honour ``max_lines_per_unit`` as a hard upper bound.
    """

    STRUCTURAL = "structural"
    LINES = "lines"

    @classmethod
    def from_value(cls, value: str) -> "ChunkStrategy":
        normalised = value.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        accepted = ", ".join(m.value for m in cls)
        raise ValueError(
            f"unknown chunk strategy {value!r}; expected one of: {accepted}"
        )


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
    VERIFYING = "verifying"    # merged output is checked (and repaired if needed)
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
