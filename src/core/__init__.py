"""Core orchestration logic for the Polyglot Swarm (Track A).

The public surface other tracks build on:

* :class:`~core.chunker.Chunker`        -- cut source files into translatable units
* :class:`~core.orchestrator.Orchestrator` -- drive a job through its lifecycle
* :func:`~core.assembler.assemble_job`  -- tape translated chapters back together
"""

from __future__ import annotations

from core.assembler import AssembledFile, assemble_job
from core.chunker import Chunker
from core.errors import (
    AssemblyError,
    ChunkingError,
    MergeError,
    OrchestrationError,
    PolyglotSwarmError,
)
from core.merger import MergedFile, MergeFn, Merger, assemble_merged
from core.orchestrator import Orchestrator, TranslateFn

__all__ = [
    "AssembledFile",
    "AssemblyError",
    "Chunker",
    "ChunkingError",
    "MergeError",
    "MergeFn",
    "MergedFile",
    "Merger",
    "Orchestrator",
    "OrchestrationError",
    "PolyglotSwarmError",
    "TranslateFn",
    "assemble_job",
    "assemble_merged",
]
