"""SQLite-backed data-access layer for the Polyglot Swarm (Track A).

Repositories translate between the pure model contracts and persistent rows.
Nothing above this layer touches ``sqlite3`` directly.
"""

from __future__ import annotations

from db.connection import Database
from db.repository import JobRepository, ResultRepository

__all__ = ["Database", "JobRepository", "ResultRepository"]
