"""Thin, safe wrapper around a SQLite connection.

Centralises connection setup (row factory, foreign keys) and schema creation so
repositories never configure the driver themselves.

A :class:`sqlite3.Connection` must not be shared between threads, so background
work (the :class:`~services.job_runner.JobRunner`) never borrows the request
thread's connection — it opens its own with :meth:`Database.sibling`. For an
on-disk database that is just a second ``sqlite3.connect``; ``":memory:"`` is
mapped to a private *shared-cache* URI so siblings reach the same in-memory
database instead of a fresh empty one (which keeps tests honest without forcing
them onto the filesystem).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.errors import RepositoryError

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# How long a connection waits for a writer to release a file lock before it
# gives up; a background run and a polling reader routinely overlap.
_BUSY_TIMEOUT_MS = 5000


class Database:
    """Owns a single SQLite connection and applies the schema.

    Use ``":memory:"`` as the path for tests. Call :meth:`close` when done, or
    use the instance as a context manager.
    """

    def __init__(self, path: str = "polyglot_swarm.db", *, _dsn: str | None = None) -> None:
        self._path = path
        # ":memory:" would give every connection its own private database, so
        # give this instance a uniquely named shared-cache one instead; the
        # name is what siblings reconnect to. It lives only as long as at least
        # one connection to it is open.
        if _dsn is not None:
            self._dsn = _dsn
        elif path == ":memory:":
            self._dsn = f"file:polyglot-mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
        else:
            self._dsn = path
        self._uri = self._dsn.startswith("file:")
        try:
            self._conn = sqlite3.connect(self._dsn, uri=self._uri)
        except sqlite3.Error as exc:  # pragma: no cover - driver-level failure
            raise RepositoryError(f"could not open database {path!r}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        if self._uri:
            # Shared-cache takes table-level locks, so let readers see through
            # a writer's transaction rather than block on SQLITE_LOCKED.
            self._conn.execute("PRAGMA read_uncommitted = 1")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def path(self) -> str:
        return self._path

    def sibling(self) -> "Database":
        """Open a second, independent connection to the *same* database.

        This is how a worker thread gets a connection it may legally use: the
        caller owns it and must :meth:`close` it when the thread ends.
        """
        return Database(self._path, _dsn=self._dsn)

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
