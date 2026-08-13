"""Test package bootstrap.

Puts ``src/`` on ``sys.path`` so tests import the Track A packages the same way
the shipped entry points do (``from models... import ...``), without needing an
editable install. ``scripts/verify.sh`` also exports PYTHONPATH for direct runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
