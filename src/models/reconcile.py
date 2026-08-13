"""Contracts for the *cross-file* reconciliation pass.

The merge tree fixes seams **inside** one file; the contract pass stops files
from diverging in the first place. This is the third and last coherence step,
and it covers what neither of those can: what the agents *actually emitted*.
A contract is a promise made before translation, and promises get broken — a
file ends up calling `calc_net_pay` when the agreed name was `calculateNetPay`,
or two files each emit their own copy of a helper.

So after merging, each file is shown the **surface** of every other file — the
symbols they really do define — alongside the contract, and given one chance to
align with them.

Two things make this different from a merge, and they are why it is not folded
through the merge tree:

* it is **per file, not pairwise** — reconciling file A against file B must
  yield *two* files, and a merge yields one, so folding files up the tree would
  concatenate distinct output files into a single blob;
* it is **advisory** — a failed reconciliation leaves the file exactly as merged
  rather than failing the job, because unpolished output is still correct output
  and the verification gate runs afterwards either way.
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
class FileSurface:
    """What one translated file actually declares, as emitted."""

    source_file_id: str
    source_path: str
    symbols: tuple[str, ...] = ()

    def render(self) -> str:
        names = ", ".join(self.symbols) if self.symbols else "(nothing public)"
        return f"{self.source_path}: {names}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "source_path": self.source_path,
            "symbols": list(self.symbols),
        }


@dataclass(frozen=True, slots=True)
class ReconcileTask:
    """One merged file, plus everything it has to agree with."""

    source_file_id: str
    source_path: str
    target_language: Language
    content: str
    others: tuple[FileSurface, ...] = ()
    contract: Contract | None = None

    def render_context(self) -> str:
        """The cross-file picture as prompt text: contract, then neighbours."""
        blocks: list[str] = []
        if self.contract is not None and not self.contract.is_empty:
            blocks.append(
                "Agreed contract:\n"
                + self.contract.render(focus_path=self.source_path)
            )
        if self.others:
            blocks.append(
                "Symbols the other translated files actually define:\n"
                + "\n".join(f"- {s.render()}" for s in self.others)
            )
        return "\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """The outcome of aligning one file with the rest of the codebase.

    ``changed`` is set by the :class:`~core.reconciler.Reconciler` by comparing
    the returned content with what went in, so a seam cannot claim an edit it
    did not make. ``success=False`` keeps the original content and is reported,
    not raised — see the module docstring.
    """

    source_file_id: str
    target_language: Language
    content: str
    agent_id: str | None = None
    changed: bool = False
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
            raise ValueError("a successful reconciliation must not carry an error")
        if not self.success and not self.error:
            raise ValueError("a failed reconciliation must carry an error message")

    @classmethod
    def failure(
        cls,
        source_file_id: str,
        target_language: Language,
        content: str,
        error: str,
        *,
        agent_id: str | None = None,
    ) -> "ReconcileResult":
        """A failed pass that hands the *unchanged* file back."""
        return cls(
            source_file_id=source_file_id,
            target_language=target_language,
            content=content,
            agent_id=agent_id,
            success=False,
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "target_language": self.target_language.value,
            "agent_id": self.agent_id,
            "changed": self.changed,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }
