"""Thin, safe wrapper around a SQLite connection.

Centralises connection setup (row factory, foreign keys) and schema creation so
repositories never configure the driver themselves.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.errors import RepositoryError

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    """Owns a single SQLite connection and applies the schema.

    Use ``":memory:"`` as the path for tests. Call :meth:`close` when done, or
    use the instance as a context manager.
    """

    def __init__(self, path: str = "polyglot_swarm.db") -> None:
        self._path = path
        try:
            self._conn = sqlite3.connect(path)
        except sqlite3.Error as exc:  # pragma: no cover - driver-level failure
            raise RepositoryError(f"could not open database {path!r}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        """Create tables/indexes if they do not yet exist (idempotent)."""
        try:
            sql = _SCHEMA_PATH.read_text(encoding="utf-8")
            self._conn.executescript(sql)
            self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise RepositoryError(f"failed to apply schema: {exc}") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; commit on success, roll back on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
