"""Contracts for the *verification* gate over merged output.

Merging reconciles chapters, but a reconciler agent can still hand back code
that does not parse. The gate checks each merged file and, if it fails, asks a
*repair* agent to fix it before the file is allowed through to assembly.

``VerificationResult`` records the outcome per file; ``RepairRequest`` is what a
repair agent receives (the broken content plus the diagnostics that condemned
it). The check itself and the repair are seams — deterministic stand-ins run in
tests and demos; a Groq-backed toolchain plugs in for production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.enums import Language


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of verifying one merged file (after any repair attempts)."""

    source_file_id: str
    ok: bool
    errors: tuple[str, ...] = ()
    attempts: int = 0  # repair rounds performed before this outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "ok": self.ok,
            "errors": list(self.errors),
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class RepairRequest:
    """A failing merged file handed to a repair agent, with its diagnostics."""

    source_file_id: str
    target_language: Language
    content: str
    errors: tuple[str, ...]
