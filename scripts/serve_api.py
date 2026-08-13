#!/usr/bin/env python3
"""Run the Track B HTTP API + web UI over a real (on-disk) SQLite database.

If ``GROQ_API_KEY`` is set, the translate / merge / repair seams are backed by a
real Groq client; otherwise they fall back to the offline stubs, so this still
serves fully working endpoints with **zero network access and no API key**. The
verification gate (``ast.parse``) is local either way.

Usage:
    python scripts/serve_api.py            # 127.0.0.1:8000
    GROQ_API_KEY=... python scripts/serve_api.py   # real translations
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
from services.groq_brain import maybe_groq_seams  # noqa: E402

_logger = logging.getLogger("polyglot.api")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    settings = load_settings()

    db = Database(settings.database_path)
    db.init_schema()

    translate_fn, merge_fn, repair_fn = maybe_groq_seams(settings)
    _logger.info(
        "Brain: %s (model=%s)",
        "Groq" if translate_fn else "offline stub",
        settings.groq.model,
    )
    app = build_app(
        db,
        settings=settings,
        translate_fn=translate_fn,
        merge_fn=merge_fn,
        repair_fn=repair_fn,
    )

    host = os.environ.get("POLYGLOT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("POLYGLOT_API_PORT", "8000"))
    try:
        serve(app, host=host, port=port)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
