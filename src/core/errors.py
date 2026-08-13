"""Exception hierarchy for Track A.

A single root (:class:`PolyglotSwarmError`) lets callers catch everything the
core raises without also swallowing unrelated ``ValueError``/``KeyError`` bugs.
"""

from __future__ import annotations


class PolyglotSwarmError(Exception):
    """Base class for all errors raised by Polyglot Swarm core code."""


class ChunkingError(PolyglotSwarmError):
    """Raised when a source file cannot be split into valid units."""


class OrchestrationError(PolyglotSwarmError):
    """Raised for illegal job-lifecycle transitions or dispatch failures."""


class MergeError(PolyglotSwarmError):
    """Raised when two translated chapters cannot be reconciled into one."""


class ReconcileError(PolyglotSwarmError):
    """Raised when the cross-file reconciliation pass cannot be run at all.

    Note that a *failed reconciliation of one file* is not an error: it is
    reported and the merged content is kept. This is for the pass itself being
    misconfigured (no agents, bad concurrency).
    """


class VerificationError(PolyglotSwarmError):
    """Raised when merged output fails to verify (and cannot be repaired)."""


class AssemblyError(PolyglotSwarmError):
    """Raised when translated units cannot be reassembled into a file."""


class ConfigError(PolyglotSwarmError):
    """Raised when configuration is missing or invalid."""


class RepositoryError(PolyglotSwarmError):
    """Raised for data-access failures in the DB layer."""
