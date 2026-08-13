"""The shared vocabulary every agent translates *against*.

This is the answer to the hardest problem in the whole pipeline. Chapters are
translated independently and in parallel, which is what makes the swarm fast —
and is also why chapter 3 calls it ``calculateNetPay`` while chapter 7 calls it
``calc_net_pay`` and file B imports a class file A never emitted under that
name. The merge tree can reconcile a *seam between two neighbours*, but it only
ever sees one file, so cross-file divergence is something it structurally
cannot fix. Repairing that afterwards is the hard direction.

So don't create the divergence. Before a single chapter is translated, one pass
over the whole codebase produces a :class:`Contract`: the public symbols, the
name each one will have in the target language, and its target-language
signature. That table is then handed to *every* translate and merge call as
shared context, so agents are not guessing independently — they are all reading
from the same sheet.

The contract is a plain data contract; the intelligence that fills it in is the
``extract_contract_fn`` seam, exactly like the other three. A deterministic
offline stub ships alongside a Groq-backed implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.enums import Language

# How much of the contract is worth putting in one prompt. A table longer than
# this is truncated with a marker rather than blowing the context window.
_DEFAULT_RENDER_LIMIT = 60


@dataclass(frozen=True, slots=True)
class ContractSymbol:
    """One public symbol and the shape it must take in the target language."""

    source_name: str
    target_name: str
    kind: str = "function"  # function | class | type | constant | module
    signature: str = ""
    source_path: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.source_name.strip():
            raise ValueError("ContractSymbol.source_name must be non-empty")
        if not self.target_name.strip():
            raise ValueError("ContractSymbol.target_name must be non-empty")

    def render(self) -> str:
        """One line of the symbol table, as an agent will read it."""
        arrow = f"{self.source_name} -> {self.target_name}"
        parts = [f"{self.kind}: {arrow}"]
        if self.signature:
            parts.append(f"as `{self.signature}`")
        if self.notes:
            parts.append(f"({self.notes})")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "target_name": self.target_name,
            "kind": self.kind,
            "signature": self.signature,
            "source_path": self.source_path,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContractSymbol":
        return cls(
            source_name=str(data["source_name"]),
            target_name=str(data["target_name"]),
            kind=str(data.get("kind", "function")),
            signature=str(data.get("signature", "")),
            source_path=str(data.get("source_path", "")),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True, slots=True)
class Contract:
    """The whole shared symbol table for one job, plus global conventions."""

    source_language: Language
    target_language: Language
    symbols: tuple[ContractSymbol, ...] = ()
    conventions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.symbols and not self.conventions

    def __len__(self) -> int:
        return len(self.symbols)

    def symbols_for(self, source_path: str) -> tuple[ContractSymbol, ...]:
        """The symbols declared by one file."""
        return tuple(s for s in self.symbols if s.source_path == source_path)

    def render(
        self, *, focus_path: str = "", limit: int = _DEFAULT_RENDER_LIMIT
    ) -> str:
        """The contract as prompt text, with ``focus_path``'s symbols first.

        A chapter's own file matters most, so those symbols lead and survive
        truncation; the rest follow as the cross-file vocabulary the chapter has
        to agree with.
        """
        if self.is_empty:
            return ""

        own = self.symbols_for(focus_path) if focus_path else ()
        others = tuple(s for s in self.symbols if s not in own)
        lines: list[str] = []
        if self.conventions:
            lines.append("Conventions:")
            lines.extend(f"- {c}" for c in self.conventions)
        if own:
            lines.append(f"Symbols declared in {focus_path}:")
            lines.extend(f"- {s.render()}" for s in own[:limit])
        remaining = max(0, limit - len(own))
        if others and remaining:
            lines.append("Symbols from the rest of the codebase:")
            lines.extend(f"- {s.render()}" for s in others[:remaining])
            if len(others) > remaining:
                lines.append(f"- ... and {len(others) - remaining} more")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_language": self.source_language.value,
            "target_language": self.target_language.value,
            "symbols": [s.to_dict() for s in self.symbols],
            "conventions": list(self.conventions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contract":
        return cls(
            source_language=Language.from_value(data["source_language"]),
            target_language=Language.from_value(data["target_language"]),
            symbols=tuple(
                ContractSymbol.from_dict(s) for s in data.get("symbols", [])
            ),
            conventions=tuple(data.get("conventions", [])),
        )
