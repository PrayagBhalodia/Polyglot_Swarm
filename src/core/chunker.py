"""Cut legacy source files into translatable units ("chapters").

This is the "pair of scissors" from the analogy. Chunking is deterministic and
line-based: given the same file and settings, it always produces the same units
with the same boundaries, which is what makes re-ingestion idempotent.
"""

from __future__ import annotations

from core.errors import ChunkingError
from models.enums import Language
from models.source import SourceFile, TranslationUnit


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
    """

    def __init__(self, max_lines_per_unit: int = 200, overlap_lines: int = 0) -> None:
        if max_lines_per_unit < 1:
            raise ValueError("max_lines_per_unit must be >= 1")
        if overlap_lines < 0:
            raise ValueError("overlap_lines must be >= 0")
        if overlap_lines >= max_lines_per_unit:
            raise ValueError("overlap_lines must be smaller than max_lines_per_unit")
        self.max_lines_per_unit = max_lines_per_unit
        self.overlap_lines = overlap_lines

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

        units: list[TranslationUnit] = []
        step = self.max_lines_per_unit - self.overlap_lines
        index = start_index
        cursor = 0
        total = len(lines)

        while cursor < total:
            end = min(cursor + self.max_lines_per_unit, total)
            body = "".join(lines[cursor:end])
            units.append(
                TranslationUnit(
                    job_id=job_id,
                    source_file_id=source_file.id,
                    index=index,
                    content=body,
                    source_language=source_file.language,
                    target_language=target_language,
                    start_line=cursor + 1,
                    end_line=end,
                )
            )
            index += 1
            if end == total:
                break
            cursor += step

        return units

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
