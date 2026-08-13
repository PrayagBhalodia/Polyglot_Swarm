"""Cut legacy source files into translatable units ("chapters").

This is the "pair of scissors" from the analogy. Chunking is deterministic:
given the same file and settings it always produces the same units with the
same boundaries, which is what makes re-ingestion idempotent.

*Where* it cuts matters, though. A fixed ``max_lines_per_unit`` split happily
slices a function in half, and half a function is the worst thing you can hand a
translator: it has to guess the rest, and the chapter either side of the seam
guesses differently. So the default strategy is **structural** — the cut is
pulled back to the nearest top-level boundary that still fits inside the budget:

* a top-level declaration (``def``/``class``/``func``/``fn``/``function``,
  ``struct``/``impl``/``interface``/``type``, a COBOL ``DIVISION``/``SECTION``,
  a Python decorator, …), or
* a top-level line that follows a blank one — the language-agnostic "new
  paragraph starts here" signal.

``max_lines_per_unit`` remains a hard upper bound: when no boundary fits (a
2,000-line function, minified code, a file with no structure at all) the cut
falls back to the plain line split, so a unit is never empty and never
oversized. ``ChunkStrategy.LINES`` selects that original behaviour outright.
"""

from __future__ import annotations

from core.errors import ChunkingError
from models.enums import ChunkStrategy, Language
from models.source import SourceFile, TranslationUnit

# Line prefixes that begin a *top-level* construct. Matched case-insensitively
# against the stripped line, so one table covers the whole language zoo; a false
# positive only means an earlier (still legal) cut, never a broken unit.
_DECLARATION_PREFIXES: tuple[str, ...] = (
    # Callables and types across the C / Python / Go / Rust / JS families.
    "def ", "async def ", "class ", "func ", "fn ", "function ", "sub ",
    "struct ", "enum ", "union ", "impl ", "trait ", "interface ", "record ",
    "type ", "typedef ", "protocol ", "extension ", "object ", "data ",
    # Visibility / storage modifiers that precede the above.
    "pub ", "public ", "private ", "protected ", "internal ", "static ",
    "final ", "abstract ", "override ", "open ", "sealed ", "inline ",
    "extern ", "unsafe ", "export ", "declare ", "const ", "var ", "let ",
    "val ", "template", "@", "#include", "#define", "#pragma",
    # Module-level structure.
    "package ", "import ", "from ", "using ", "namespace ", "module ",
    "require ", "include ",
    # Legacy: COBOL divisions/sections, Fortran/Pascal/VB program units.
    "identification ", "environment ", "data ", "procedure ", "working-storage",
    "program ", "subroutine ", "end ", "begin", "unit ", "uses ", "implementation",
    "property ", "dim ", "attribute ",
)

# Annotations that *belong to* the declaration underneath them (Python/Java
# decorators, Rust attributes). A cut may open on one, never between one and
# what it annotates.
_ANNOTATION_PREFIXES: tuple[str, ...] = ("@", "#[")

# Languages whose "column 0" is not really column 0: fixed-form COBOL and
# Fortran reserve the first columns for sequence numbers and continuations, so
# a construct starting a little way in is still top level.
_TOP_LEVEL_INDENT: dict[Language, int] = {
    Language.COBOL: 7,
    Language.FORTRAN: 6,
}

# Trailing words that mark a top-level COBOL construct regardless of prefix.
_DIVISION_SUFFIXES: tuple[str, ...] = ("division.", "section.")


class Chunker:
    """Splits a :class:`SourceFile` into a contiguous list of units.

    Parameters
    ----------
    max_lines_per_unit:
        Hard cap on the number of source lines in one unit. Must be >= 1.
    overlap_lines:
        Number of trailing lines from the previous unit to repeat at the start
        of the next one. Overlap gives each agent a little surrounding context
        so translations join cleanly; it must be smaller than
        ``max_lines_per_unit``. Overlapped lines are stripped again at assembly.
    strategy:
        ``STRUCTURAL`` (the default) pulls each cut back to the nearest
        top-level boundary that still fits the budget; ``LINES`` splits purely
        by count. Both are deterministic.
    """

    def __init__(
        self,
        max_lines_per_unit: int = 200,
        overlap_lines: int = 0,
        *,
        strategy: ChunkStrategy = ChunkStrategy.STRUCTURAL,
    ) -> None:
        if max_lines_per_unit < 1:
            raise ValueError("max_lines_per_unit must be >= 1")
        if overlap_lines < 0:
            raise ValueError("overlap_lines must be >= 0")
        if overlap_lines >= max_lines_per_unit:
            raise ValueError("overlap_lines must be smaller than max_lines_per_unit")
        self.max_lines_per_unit = max_lines_per_unit
        self.overlap_lines = overlap_lines
        self.strategy = strategy

    def chunk_file(
        self,
        source_file: SourceFile,
        *,
        job_id: str,
        target_language: Language,
        start_index: int = 0,
    ) -> list[TranslationUnit]:
        """Return the units for one file, numbered from ``start_index``.

        ``start_index`` lets a caller chunk many files into a single global,
        gap-free index sequence for a job.
        """
        if target_language == source_file.language:
            raise ChunkingError(
                "target language must differ from source language "
                f"({source_file.language.value})"
            )

        lines = source_file.content.splitlines(keepends=True)
        if not lines:
            raise ChunkingError(f"source file {source_file.path!r} is empty")

        boundaries = (
            _boundaries(lines, source_file.language)
            if self.strategy == ChunkStrategy.STRUCTURAL
            else frozenset[int]()
        )

        units: list[TranslationUnit] = []
        index = start_index
        cursor = 0
        total = len(lines)

        while cursor < total:
            end = self._cut(cursor, total, boundaries)
            units.append(
                TranslationUnit(
                    job_id=job_id,
                    source_file_id=source_file.id,
                    index=index,
                    content="".join(lines[cursor:end]),
                    source_language=source_file.language,
                    target_language=target_language,
                    start_line=cursor + 1,
                    end_line=end,
                    # Run-time context so a seam can say *which file* this
                    # chapter came from, not just what it contains.
                    source_path=source_file.path,
                )
            )
            index += 1
            if end == total:
                break
            # Step back by the overlap for join context, but always forward.
            cursor = max(cursor + 1, end - self.overlap_lines)

        return units

    def _cut(self, cursor: int, total: int, boundaries: frozenset[int]) -> int:
        """Where the unit starting at ``cursor`` should end (exclusive).

        Never returns ``<= cursor`` (no empty units) and never more than
        ``max_lines_per_unit`` past it (the budget is a hard bound).
        """
        limit = min(cursor + self.max_lines_per_unit, total)
        if limit >= total:
            return total
        # The best cut is the last boundary that still fits; a boundary line
        # opens the *next* unit, so anything in (cursor, limit] is fair game.
        candidates = [b for b in boundaries if cursor < b <= limit]
        return max(candidates) if candidates else limit

    def chunk_files(
        self,
        source_files: list[SourceFile],
        *,
        job_id: str,
        target_language: Language,
    ) -> list[TranslationUnit]:
        """Chunk many files into one continuous, gap-free unit sequence."""
        all_units: list[TranslationUnit] = []
        for source_file in source_files:
            units = self.chunk_file(
                source_file,
                job_id=job_id,
                target_language=target_language,
                start_index=len(all_units),
            )
            all_units.extend(units)
        if not all_units:
            raise ChunkingError("no units produced; source file list was empty")
        return all_units


def _boundaries(lines: list[str], language: Language) -> frozenset[int]:
    """Indices of lines that begin a new top-level construct.

    Index ``0`` is never included: a unit has to start somewhere, and cutting
    before the first line would produce an empty one.
    """
    allowed_indent = _TOP_LEVEL_INDENT.get(language, 0)
    found: set[int] = set()
    for i in range(1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue  # a blank line ends a block; the next line starts one
        if len(line) - len(line.lstrip()) > allowed_indent:
            continue  # indented: we are inside a body, never cut here
        previous = lines[i - 1].strip()
        if previous.startswith(_ANNOTATION_PREFIXES):
            continue  # a decorator/attribute must stay with what it annotates
        if not previous or _is_declaration(stripped):
            found.add(i)
    return frozenset(found)


def _is_declaration(stripped: str) -> bool:
    """Does this (already stripped) line open a named top-level construct?"""
    lowered = stripped.lower()
    if lowered.endswith(_DIVISION_SUFFIXES):
        return True
    return lowered.startswith(_DECLARATION_PREFIXES)
