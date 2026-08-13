"""The default, offline reconciliation seam for the API.

The :class:`~core.merger.Merger` reconciles two adjacent chapters through a
``merge_fn`` seam — a second Groq call in production. So jobs can run end-to-end
in tests and demos without a network or API key, this ships a deterministic
stub: it joins the two pieces in order and collapses the blank lines at the
seam, standing in for a real reconciling agent. A production deployment injects
a Groq-backed ``merge_fn`` in its place; nothing else in the pipeline changes.
"""

from __future__ import annotations

from models.agent import SwarmAgent
from models.merge import MergeResult, MergeTask


def stub_merge(task: MergeTask, agent: SwarmAgent) -> MergeResult:
    """Order-preserving join of two chapters, with the seam tidied (no network)."""
    left = task.left.rstrip("\n")
    right = task.right.lstrip("\n")
    if not left:
        merged = right
    elif not right:
        merged = left
    else:
        merged = f"{left}\n{right}"
    return MergeResult(
        source_file_id=task.source_file_id,
        target_language=task.target_language,
        merged=merged,
        agent_id=agent.id,
        tokens_used=len(task.left) + len(task.right),
        duration_ms=1,
    )
