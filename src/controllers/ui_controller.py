"""Serve the single-page web UI.

The page is a self-contained HTML/CSS/JS file (no external assets, matching the
stdlib-only, zero-dependency ethos). It is read once at import so requests just
hand back the cached markup with a ``text/html`` content type.
"""

from __future__ import annotations

from pathlib import Path

from api.http import Request, Response

_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
_HTML = _INDEX.read_text(encoding="utf-8")


def index(request: Request) -> Response:
    """Return the Polyglot Swarm web UI."""
    return Response(200, _HTML, content_type="text/html; charset=utf-8")
