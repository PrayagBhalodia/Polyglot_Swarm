"""Align the finished files with each other, once they all exist.

This is the last of the three coherence mechanisms, and the only one that gets
to see the *whole translated codebase at once*:

1. the **contract** pass agrees the vocabulary before anything is translated,
2. the **merge** tree fixes the seams between chapters within each file,
3. **reconciliation** — here — checks the finished files against one another.

The first two are preventative and local. Neither can catch a file that quietly
broke the promise: the contract said ``calculateNetPay``, chapter 6 of file B
wrote ``calc_net_pay`` anyway, and the merge tree never noticed because it only
ever looks inside one file. Here every file is shown the *surface* of the
others — the symbols they genuinely declare, scanned from the emitted code, not
from the plan — and given one pass to agree with them.

The shape deliberately mirrors :class:`~core.merger.Merger`: a seam
(``reconcile_fn``) dispatched round-robin across the swarm, bounded concurrency,
results collected in submission order so the output never depends on which
agent finished first. What it does *not* reuse is the pairwise fold, and for a
concrete reason: a merge turns two pieces into one, and files must stay
separate — folding files up the tree would tape a whole codebase into a single
blob.

Reconciliation is **advisory**. A file whose pass fails keeps exactly the
content the merge tree produced, and the job carries on to verification: an
unpolished file is still a correct file, and refusing to emit one because a
cosmetic rename call failed would be a worse outcome than the problem.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

from core.errors import ReconcileError
from core.merger import MergedFile
from models.agent import SwarmAgent
from models.contract import Contract
from models.enums import AgentStatus, Language
from models.job import TranslationJob
from models.reconcile import FileSurface, ReconcileResult, ReconcileTask

# The cross-file seam: align one finished file with the rest of the codebase.
ReconcileFn = Callable[[ReconcileTask, SwarmAgent], ReconcileResult]

# How a file's declared symbols are read back out of the translated code.
SurfaceScanner = Callable[[str, Language], list[tuple[str, str]]]


class Reconciler:
    """Aligns a job's merged files with each other.

    Parameters
    ----------
    reconcile_fn:
        The seam that aligns one file. Groq-backed in production; a
        deterministic stub stands in for tests and demos.
    agents:
        The swarm the work is dispatched across (round-robin over the online
        agents), as with translation and merging.
    scanner:
        Reads the symbols a translated file declares. Injected so the core stays
        independent of the services layer's language heuristics.
    max_concurrency:
        How many files may be reconciled at once. ``1`` keeps it sequential.
    """

    def __init__(
        self,
        reconcile_fn: ReconcileFn,
        agents: Sequence[SwarmAgent],
        *,
        scanner: SurfaceScanner,
        max_concurrency: int = 1,
    ) -> None:
        if not agents:
            raise ReconcileError("reconciler requires at least one agent")
        if max_concurrency < 1:
            raise ReconcileError("max_concurrency must be >= 1")
        self._reconcile_fn = reconcile_fn
        self._agents = list(agents)
        self._scanner = scanner
        self._max_concurrency = max_concurrency

    def reconcile(
        self,
        job: TranslationJob,
        merged_files: Sequence[MergedFile],
        *,
        contract: Contract | None = None,
    ) -> tuple[list[MergedFile], list[ReconcileResult]]:
        """Align every file with the others; returns the files and a per-file result.

        A single file has nothing to be inconsistent *with*, so it is returned
        untouched and unbilled.
        """
        if len(merged_files) < 2:
            return list(merged_files), []

        paths = {f.id: f.path for f in job.source_files}
        surfaces = [
            self._surface(mf, paths, job.target_language) for mf in merged_files
        ]
        # Agents are chosen here, on the calling thread, so the round-robin is
        # never raced — the same discipline the merge tree and the gate follow.
        pool = self._online_agents()
        tasks = [
            (
                ReconcileTask(
                    source_file_id=mf.source_file_id,
                    source_path=surfaces[i].source_path,
                    target_language=job.target_language,
                    content=mf.content,
                    others=tuple(s for j, s in enumerate(surfaces) if j != i),
                    contract=contract,
                ),
                pool[i % len(pool)],
            )
            for i, mf in enumerate(merged_files)
        ]

        outcomes = self._run_all(tasks)

        files: list[MergedFile] = []
        results: list[ReconcileResult] = []
        for (task, _), result in zip(tasks, outcomes):
            # A failed pass keeps what the merge tree produced; the seam is not
            # trusted to report whether it changed anything, either.
            content = result.content if result.success else task.content
            files.append(replace(merged_files[len(files)], content=content))
            results.append(
                replace(result, content=content, changed=content != task.content)
            )
        return files, results

    # --- Internals ----------------------------------------------------------

    def _surface(
        self, mf: MergedFile, paths: dict[str, str], language: Language
    ) -> FileSurface:
        return FileSurface(
            source_file_id=mf.source_file_id,
            source_path=paths.get(mf.source_file_id, mf.source_file_id),
            symbols=tuple(name for _, name in self._scanner(mf.content, language)),
        )

    def _online_agents(self) -> list[SwarmAgent]:
        pool = [a for a in self._agents if a.status != AgentStatus.OFFLINE]
        if not pool:
            raise ReconcileError("no online agents available for reconciliation")
        return pool

    def _run_all(
        self, tasks: Sequence[tuple[ReconcileTask, SwarmAgent]]
    ) -> list[ReconcileResult]:
        if len(tasks) <= 1 or self._max_concurrency <= 1:
            return [self._reconcile_fn(task, agent) for task, agent in tasks]

        workers = min(self._max_concurrency, len(tasks))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="polyglot-reconcile"
        ) as executor:
            futures = [
                executor.submit(partial(self._reconcile_fn, task, agent))
                for task, agent in tasks
            ]
            # Indexed, not as_completed: file order must not depend on timing.
            return [future.result() for future in futures]
