#!/usr/bin/env python3
"""Create (or migrate) the Polyglot Swarm SQLite database.

Reads the DB path from settings (env/TOML aware) and applies ``schema.sql``
idempotently. Safe to run repeatedly.

Usage:
    python scripts/init_db.py [--config path/to/user.toml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the Track A packages importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config.settings import load_settings  # noqa: E402
from db.connection import Database  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the Polyglot Swarm DB")
    parser.add_argument(
        "--config",
        help="optional user TOML config overriding packaged defaults",
        default=None,
    )
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    with Database(settings.database_path) as db:
        db.init_schema()
    print(f"Schema applied to {settings.database_path!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
