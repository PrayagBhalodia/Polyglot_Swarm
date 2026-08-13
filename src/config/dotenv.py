"""A minimal, dependency-free ``.env`` loader.

Reads ``KEY=VALUE`` lines from a ``.env`` file and populates ``os.environ`` — but
never overrides a variable already set in the real environment, so an exported
value always wins. Deliberately tiny: comments (``#``) and blank lines are
skipped, a leading ``export`` is tolerated, and surrounding quotes are stripped;
there is no variable interpolation.

This is an *application bootstrap* concern, so it is called from entry-point
scripts (e.g. ``serve_api.py``) rather than from :func:`config.settings.load_settings`,
which must stay a pure function of the environment for testability.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/config/dotenv.py -> src/config -> src -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: str | Path | None = None) -> None:
    """Load ``path`` (default: the project-root ``.env``) into ``os.environ``.

    Missing files are a no-op, so this is always safe to call.
    """
    env_path = Path(path) if path is not None else _PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
