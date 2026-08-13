"""Turn a *source location* into the ``source_files`` a job is created from.

The UI lets a user point at either a local folder or a public GitHub repo; this
module resolves either into a list of ``{"path", "content"}`` entries filtered to
the chosen source language. Everything here is stdlib-only:

* **local** — walk the directory and read matching text files.
* **github** — ``git clone --depth 1`` into a temp dir (the system ``git``), then
  walk it exactly like a local folder, and clean up.

Reading files touches the filesystem and cloning touches the network, so this
lives in the service layer, never in the tested-offline core. User-facing
problems raise :class:`ValueError` with an actionable message (the API maps that
to ``400``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from models.enums import Language

# File extensions that identify each source language.
_EXTENSIONS: dict[Language, tuple[str, ...]] = {
    Language.PYTHON: (".py", ".pyw"),
    Language.JAVASCRIPT: (".js", ".mjs", ".cjs", ".jsx"),
    Language.TYPESCRIPT: (".ts", ".tsx"),
    Language.JAVA: (".java",),
    Language.C: (".c", ".h"),
    Language.CPP: (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    Language.CSHARP: (".cs",),
    Language.GO: (".go",),
    Language.RUST: (".rs",),
    Language.RUBY: (".rb",),
    Language.PHP: (".php",),
    Language.SWIFT: (".swift",),
    Language.KOTLIN: (".kt", ".kts"),
    Language.COBOL: (".cob", ".cbl", ".cpy"),
    Language.FORTRAN: (".f", ".for", ".f90", ".f95", ".f03"),
    Language.PERL: (".pl", ".pm"),
    Language.VB6: (".bas", ".cls", ".frm"),
    Language.DELPHI: (".pas", ".dpr"),
}

_MAX_FILES = 500
_MAX_FILE_BYTES = 1_000_000  # skip anything larger than ~1 MB
_GITHUB_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?$")
_CLONE_TIMEOUT_SECONDS = 120


def ingest_source(
    *, kind: str, location: str, source_language: Language
) -> list[dict[str, Any]]:
    """Resolve a source location into ``{"path", "content"}`` file entries."""
    location = location.strip()
    if not location:
        raise ValueError("a source location (path or GitHub URL) is required")

    if kind == "local":
        return _ingest_dir(Path(location).expanduser(), source_language)
    if kind == "github":
        return _ingest_github(location, source_language)
    raise ValueError(f"unknown source kind {kind!r}; expected 'local' or 'github'")


def _ingest_github(url: str, language: Language) -> list[dict[str, Any]]:
    if not _GITHUB_URL.match(url):
        raise ValueError(
            "expected a public GitHub URL like https://github.com/owner/repo"
        )
    if shutil.which("git") is None:
        raise ValueError("git is not installed; cannot clone a GitHub repository")

    tmp = tempfile.mkdtemp(prefix="polyglot-clone-")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, tmp],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["clone failed"]
            raise ValueError(f"could not clone {url}: {detail[0]}")
        return _ingest_dir(Path(tmp), language)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"cloning {url} timed out") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ingest_dir(root: Path, language: Language) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    extensions = _EXTENSIONS.get(language, ())
    if not extensions:
        raise ValueError(f"no known file extensions for {language.value}")

    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary — skip rather than fail the batch
        files.append({"path": os.path.relpath(path, root), "content": content})
        if len(files) >= _MAX_FILES:
            break

    if not files:
        exts = ", ".join(extensions)
        raise ValueError(
            f"no {language.value} files ({exts}) found under {root}"
        )
    return files
