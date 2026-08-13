"""Tape translated chapters back together in the right order.

This is the "manager" from the analogy who reassembles the translated chapters.
Reassembly is grouped by source file and ordered by each unit's ``index``, so
the emitted target file follows the same structure as the legacy original.

Two strategies share the same grouping/ordering front end:

* :func:`assemble_job` — the naive join: concatenate ordered pieces with a
  separator. Fast, deterministic, but the seams between chapters are untouched.
* :func:`assemble_merged` — wrap the output of the :class:`~core.merger.Merger`,
  which has already reconciled adjacent chapters pairwise (see that module).
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


def group_units_by_file(job: TranslationJob) -> dict[str, list[TranslationUnit]]:
    """Group a job's units by source file, each list ordered by ``index``.

    The single source of truth for reassembly order, shared by the naive
    assembler and the :class:`~core.merger.Merger` so the two can never disagree
    on how a file is laid out.
    """
    if not job.units:
        raise AssemblyError(f"job {job.id!r} has no units to assemble")

    by_file: dict[str, list[TranslationUnit]] = {}
    for unit in job.units:
        by_file.setdefault(unit.source_file_id, []).append(unit)
    for units in by_file.values():
        units.sort(key=lambda u: u.index)
    return by_file


def piece_for(
    unit: TranslationUnit, results: dict[str, TranslationResult]
) -> str:
    """Return one unit's translated text, or raise if it is missing/failed.

    A half-translated file is worse than an obvious error, so a missing or
    failed result aborts assembly loudly.
    """
    result = results.get(unit.id)
    if result is None:
        raise AssemblyError(
            f"missing result for unit index {unit.index} "
            f"(id={unit.id}) of file {unit.source_file_id!r}"
        )
    if not result.success:
        raise AssemblyError(
            f"unit index {unit.index} (id={unit.id}) failed to "
            f"translate: {result.error}"
        )
    return result.translated_content


def _source_paths(job: TranslationJob) -> dict[str, str]:
    return {f.id: f.path for f in job.source_files}


def assemble_job(
    job: TranslationJob,
    results: dict[str, TranslationResult],
    *,
    joiner: str = "\n",
) -> list[AssembledFile]:
    """Reassemble every source file in ``job`` by naive ordered concatenation.

    Every unit must have a *successful* result present (see :func:`piece_for`).
    ``joiner`` is placed between consecutive units of the same file. Used when
    agent-based merging is disabled; otherwise prefer :func:`assemble_merged`.
    """
    by_file = group_units_by_file(job)
    path_by_id = _source_paths(job)

    assembled: list[AssembledFile] = []
    for source_file_id, units in by_file.items():
        pieces = [piece_for(unit, results) for unit in units]
        assembled.append(
            AssembledFile(
                source_file_id=source_file_id,
                source_path=path_by_id.get(source_file_id, source_file_id),
                target_language=job.target_language,
                content=joiner.join(pieces),
                unit_count=len(units),
            )
        )
    return assembled
