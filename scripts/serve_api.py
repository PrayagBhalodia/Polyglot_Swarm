#!/usr/bin/env python3
"""Run the Track B HTTP API over a real (on-disk) SQLite database.

The translation seam defaults to the offline stub, so this serves fully working
endpoints with **zero network access and no API key** — a production deployment
swaps in a Groq-backed ``translate_fn`` via ``build_app(..., translate_fn=...)``.

Usage:
    python scripts/serve_api.py            # 127.0.0.1:8000
    POLYGLOT_API_PORT=9000 python scripts/serve_api.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api.app import build_app  # noqa: E402
from api.server import serve  # noqa: E402
from config.settings import load_settings  # noqa: E402
from db.connection import Database  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    settings = load_settings()

    db = Database(settings.database_path)
    db.init_schema()

    app = build_app(db, settings=settings)

    host = os.environ.get("POLYGLOT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("POLYGLOT_API_PORT", "8000"))
    try:
        serve(app, host=host, port=port)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
