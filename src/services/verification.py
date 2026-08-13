"""The default, offline verification seam for the API.

The :class:`~core.verifier.Verifier` gates merged output through a ``verify_fn``
check. This ships a genuine, dependency-free default:

* **Python** — the real thing: ``ast.parse`` proves the merged file is
  syntactically valid Python, and a ``SyntaxError`` becomes a precise diagnostic.
* **Other targets** — no stdlib parser exists, so fall back to a structural
  sanity check (non-empty, balanced brackets). A production deployment injects a
  toolchain-backed ``verify_fn`` (e.g. ``tsc --noEmit``, ``go vet``) in its place.

No network and no API key are needed for either path.
"""

from __future__ import annotations

import ast

from models.enums import Language

_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = set(_OPEN_TO_CLOSE.values())


def default_verify(content: str, language: Language) -> tuple[bool, list[str]]:
    """Return ``(ok, errors)`` for one merged file in ``language``."""
    if not content.strip():
        return False, ["merged output is empty"]
    if language == Language.PYTHON:
        return _verify_python(content)
    return _verify_structure(content)


def _verify_python(content: str) -> tuple[bool, list[str]]:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return False, [f"{where}: {exc.msg}"]
    return True, []


def _verify_structure(content: str) -> tuple[bool, list[str]]:
    """A cheap language-agnostic check: brackets must balance and nest."""
    stack: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for char in line:
            if char in _OPEN_TO_CLOSE:
                stack.append(_OPEN_TO_CLOSE[char])
            elif char in _CLOSERS:
                if not stack or stack.pop() != char:
                    return False, [f"line {lineno}: unbalanced '{char}'"]
    if stack:
        return False, [f"unclosed '{stack[-1]}' before end of file"]
    return True, []
