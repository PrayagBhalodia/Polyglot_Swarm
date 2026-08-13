"""Reconcile translated chapters pairwise, bottom-up, into one coherent file.

Independent chunks are translated in parallel, so neighbouring chapters can
diverge where they meet (a type named two different ways, a helper defined
twice, mismatched imports). Naively concatenating them — what
:func:`~core.assembler.assemble_job` does — leaves those seams broken.

Instead, the :class:`Merger` folds a file's ordered pieces up a **binary tree**:
adjacent pairs are handed to agents that reconcile each seam, halving the count
each level until a single piece remains. The reduction is:

* **order-preserving** — ``left`` always stays before ``right``; a merge only
  fixes the boundary between them, never reorders code;
* **log-depth** — ``ceil(log2(n))`` levels for ``n`` chapters, so the pairs at
  each level can be reconciled by as many agents as you care to configure;
* **carry-safe** — an odd piece at any level rides up unchanged to pair on the
  next, so any chapter count works.

The actual reconciliation is the ``merge_fn`` seam (a second Groq call in
production); a deterministic stub stands in for tests and demos, exactly like
the translate seam.

Every pair *at one level* is independent, so a level's merges are dispatched
concurrently (bounded by ``max_concurrency``) and the next level only starts
once they have all landed. Agents are picked, and results collected, in
submission order on the calling thread, so completion order can never change
the output: the tree is parallel but deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from core.assembler import (
    AssembledFile,
    group_units_by_file,
    piece_for,
)
from core.errors import MergeError
from models.agent import SwarmAgent
from models.contract import Contract
from models.enums import AgentStatus, Language
from models.job import TranslationJob
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult

# The reconciliation seam: fold two adjacent translated pieces into one.
MergeFn = Callable[[MergeTask, SwarmAgent], MergeResult]


@dataclass(frozen=True, slots=True)
class MergedFile:
    """One source file after its chapters have been reconciled into a whole."""

    source_file_id: str
    content: str
    leaf_count: int   # chapters that went in
    merge_count: int  # reconciliation steps performed (0 for a single chapter)
    depth: int        # height of the merge tree
    tokens_used: int


class Merger:
    """Reconciles a job's translated chapters into coherent files.

    Parameters
    ----------
    merge_fn:
        The seam that reconciles two adjacent pieces. In production this is a
        Groq-backed agent; tests inject a deterministic stub.
    agents:
        The swarm the merge tasks are dispatched across (round-robin over the
        online agents), so reconciliation uses the same workers as translation.
    max_concurrency:
        How many pairs of one tree level may be reconciled at once. ``1`` keeps
        the tree strictly sequential.
    """

    def __init__(
        self,
        merge_fn: MergeFn,
        agents: Sequence[SwarmAgent],
        *,
        max_concurrency: int = 1,
    ) -> None:
        if not agents:
            raise MergeError("merger requires at least one agent")
        if max_concurrency < 1:
            raise MergeError("max_concurrency must be >= 1")
        self._merge_fn = merge_fn
        self._agents = list(agents)
        self._max_concurrency = max_concurrency
        self._cursor = 0

    def merge_job(
        self,
        job: TranslationJob,
        results: dict[str, TranslationResult],
        *,
        contract: Contract | None = None,
    ) -> list[MergedFile]:
        """Reconcile every source file in ``job`` from its unit results.

        ``contract`` is the job-wide symbol table the chapters were translated
        against; passing it on means a reconciler resolves a disagreement at a
        seam by looking it up rather than by picking a side.
        """
        merged: list[MergedFile] = []
        paths = {f.id: f.path for f in job.source_files}
        for source_file_id, units in group_units_by_file(job).items():
            leaves = [
                (piece_for(unit, results), (unit.index, unit.index))
                for unit in units
            ]
            merged.append(
                self._reduce(
                    source_file_id,
                    job.target_language,
                    leaves,
                    source_path=paths.get(source_file_id, ""),
                    contract=contract,
                )
            )
        return merged

    # --- Internals ----------------------------------------------------------

    def _reduce(
        self,
        source_file_id: str,
        target_language: Language,
        leaves: list[tuple[str, tuple[int, int]]],
        *,
        source_path: str = "",
        contract: Contract | None = None,
    ) -> MergedFile:
        """Fold ``leaves`` (``(content, index_span)`` pairs) up the merge tree."""
        if not leaves:
            raise MergeError(f"no chapters to merge for file {source_file_id!r}")

        leaf_count = len(leaves)
        merge_count = 0
        tokens = 0
        depth = 0

        level = leaves
        while len(level) > 1:
            # Build the whole level's work first (on this thread): pairs in
            # order, each with the agent the round-robin hands it, plus a
            # possible odd piece that rides up unchanged to pair next level.
            tasks: list[tuple[MergeTask, SwarmAgent]] = []
            for i in range(0, len(level) - 1, 2):
                left, left_span = level[i]
                right, right_span = level[i + 1]
                tasks.append(
                    (
                        MergeTask(
                            source_file_id=source_file_id,
                            target_language=target_language,
                            left=left,
                            right=right,
                            left_span=left_span,
                            right_span=right_span,
                            depth=depth,
                            source_path=source_path,
                            contract=contract,
                        ),
                        self._next_agent(),
                    )
                )
            carry = [level[-1]] if len(level) % 2 else []

            results = self._run_level(tasks)

            nxt: list[tuple[str, tuple[int, int]]] = []
            for (task, _), result in zip(tasks, results):
                if not result.success:
                    raise MergeError(
                        f"failed to reconcile chapters {task.span} of file "
                        f"{source_file_id!r}: {result.error}"
                    )
                nxt.append((result.merged, task.span))
                merge_count += 1
                tokens += result.tokens_used
            level = nxt + carry
            depth += 1

        return MergedFile(
            source_file_id=source_file_id,
            content=level[0][0],
            leaf_count=leaf_count,
            merge_count=merge_count,
            depth=depth,
            tokens_used=tokens,
        )

    def _run_level(
        self, tasks: Sequence[tuple[MergeTask, SwarmAgent]]
    ) -> list[MergeResult]:
        """Reconcile one level's independent pairs, returning them *in order*."""
        if len(tasks) <= 1 or self._max_concurrency <= 1:
            return [self._merge_fn(task, agent) for task, agent in tasks]

        workers = min(self._max_concurrency, len(tasks))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="polyglot-merge"
        ) as pool:
            futures = [
                pool.submit(partial(self._merge_fn, task, agent))
                for task, agent in tasks
            ]
            # Indexing the futures list (not as_completed) keeps left-to-right
            # order regardless of which agent finishes first.
            return [future.result() for future in futures]

    def _next_agent(self) -> SwarmAgent:
        """Round-robin over agents that are participating (not OFFLINE)."""
        pool = [a for a in self._agents if a.status != AgentStatus.OFFLINE]
        if not pool:
            raise MergeError("no online agents available for merging")
        agent = pool[self._cursor % len(pool)]
        self._cursor += 1
        return agent


def assemble_merged(
    job: TranslationJob, merged_files: Sequence[MergedFile]
) -> list[AssembledFile]:
    """Wrap already-reconciled :class:`MergedFile` output as assembled files."""
    path_by_id = {f.id: f.path for f in job.source_files}
    return [
        AssembledFile(
            source_file_id=mf.source_file_id,
            source_path=path_by_id.get(mf.source_file_id, mf.source_file_id),
            target_language=job.target_language,
            content=mf.content,
            unit_count=mf.leaf_count,
        )
        for mf in merged_files
    ]
