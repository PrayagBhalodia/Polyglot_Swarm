"""Tape translated chapters back together in the right order.

This is the "manager" from the analogy who reassembles the translated chapters.
Reassembly is grouped by source file and ordered by each unit's ``index``, so
the emitted target file follows the same structure as the legacy original.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.errors import AssemblyError
from models.enums import Language
from models.job import TranslationJob
from models.result import TranslationResult
from models.source import TranslationUnit


@dataclass(frozen=True, slots=True)
class AssembledFile:
    """The fully translated counterpart of one source file."""

    source_file_id: str
    source_path: str
    target_language: Language
    content: str
    unit_count: int


def assemble_job(
    job: TranslationJob,
    results: dict[str, TranslationResult],
    *,
    joiner: str = "\n",
) -> list[AssembledFile]:
    """Reassemble every source file in ``job`` from its unit results.

    Parameters
    ----------
    job:
        The job whose units are being reassembled.
    results:
        Mapping of ``unit_id -> TranslationResult``. Every unit in the job must
        have a *successful* result present, otherwise assembly fails loudly --
        a half-translated file is worse than an obvious error.
    joiner:
        String placed between consecutive units of the same file.

    Raises
    ------
    AssemblyError
        If any unit is missing a result or its result is a failure.
    """
    if not job.units:
        raise AssemblyError(f"job {job.id!r} has no units to assemble")

    # Group units by source file, preserving order via index.
    by_file: dict[str, list[TranslationUnit]] = {}
    for unit in job.units:
        by_file.setdefault(unit.source_file_id, []).append(unit)

    path_by_id = {f.id: f.path for f in job.source_files}

    assembled: list[AssembledFile] = []
    for source_file_id, units in by_file.items():
        ordered = sorted(units, key=lambda u: u.index)
        pieces: list[str] = []
        for unit in ordered:
            result = results.get(unit.id)
            if result is None:
                raise AssemblyError(
                    f"missing result for unit index {unit.index} "
                    f"(id={unit.id}) of file {source_file_id!r}"
                )
            if not result.success:
                raise AssemblyError(
                    f"unit index {unit.index} (id={unit.id}) failed to "
                    f"translate: {result.error}"
                )
            pieces.append(result.translated_content)

        assembled.append(
            AssembledFile(
                source_file_id=source_file_id,
                source_path=path_by_id.get(source_file_id, source_file_id),
                target_language=job.target_language,
                content=joiner.join(pieces),
                unit_count=len(ordered),
            )
        )

    return assembled
