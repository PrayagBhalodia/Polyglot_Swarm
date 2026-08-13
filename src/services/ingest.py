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

Security
--------
Ingestion is the one place where a request names a path the *server* then reads,
so it is the one place that could turn into file disclosure if the API were ever
exposed. :class:`IngestPolicy` is the guard rail:

* ``root`` — an optional allow-list base directory. When set, a local path is
  fully resolved (symlinks included) and rejected unless it lands inside the
  root, which is what makes ``../../etc`` and a symlink pointing out of the tree
  fail rather than succeed. Files *found* under the root are re-checked the same
  way, so a symlinked file cannot smuggle content out either. Unset preserves
  the historical "anywhere the process can read" behaviour, which is fine for a
  local single-user run and wrong for anything shared.
* ``max_files`` / ``max_file_bytes`` — bounds on how much a single ingest can
  pull into memory.

Cloning is restricted to a strict ``https://github.com/owner/repo`` allow-list
and runs under a timeout. It also runs **no repository hooks**: a clone never
executes hooks from the cloned repo, and ``core.hooksPath`` is pinned empty so
local templates cannot inject any either, with terminal prompts disabled so a
private URL fails fast instead of hanging on a credential prompt.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings
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


@dataclass(frozen=True, slots=True)
class IngestPolicy:
    """What a single ingest is allowed to read, and how much of it."""

    root: Path | None = None
    max_files: int = _MAX_FILES
    max_file_bytes: int = _MAX_FILE_BYTES
    clone_timeout_seconds: int = _CLONE_TIMEOUT_SECONDS

    @classmethod
    def from_settings(cls, settings: Settings) -> "IngestPolicy":
        return cls(
            root=Path(settings.ingest_root).expanduser()
            if settings.ingest_root
            else None,
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
        )

    def resolved_root(self) -> Path | None:
        """The allow-list root with symlinks resolved, or ``None`` if unset."""
        if self.root is None:
            return None
        return self.root.resolve()


DEFAULT_POLICY = IngestPolicy()


def ingest_source(
    *,
    kind: str,
    location: str,
    source_language: Language,
    policy: IngestPolicy = DEFAULT_POLICY,
) -> list[dict[str, Any]]:
    """Resolve a source location into ``{"path", "content"}`` file entries."""
    location = location.strip()
    if not location:
        raise ValueError("a source location (path or GitHub URL) is required")

    if kind == "local":
        return _ingest_dir(_resolve_local(location, policy), source_language, policy)
    if kind == "github":
        return _ingest_github(location, source_language, policy)
    raise ValueError(f"unknown source kind {kind!r}; expected 'local' or 'github'")


def _resolve_local(location: str, policy: IngestPolicy) -> Path:
    """Resolve a user-supplied path and check it against the allow-list.

    ``Path.resolve`` collapses ``..`` *and* follows symlinks, so the containment
    check below sees the real target rather than the string the caller wrote.
    """
    candidate = Path(location).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:  # missing, unreadable, symlink loop
        raise ValueError(f"not a directory: {location}") from exc

    root = policy.resolved_root()
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError(
            f"path {location!r} is outside the allowed ingest root ({root})"
        )
    return resolved


def _ingest_github(
    url: str, language: Language, policy: IngestPolicy
) -> list[dict[str, Any]]:
    if not _GITHUB_URL.match(url):
        raise ValueError(
            "expected a public GitHub URL like https://github.com/owner/repo"
        )
    if shutil.which("git") is None:
        raise ValueError("git is not installed; cannot clone a GitHub repository")

    tmp = tempfile.mkdtemp(prefix="polyglot-clone-")
    try:
        result = subprocess.run(
            [
                "git",
                # No hook may run for this clone: the cloned repo's own hooks
                # are never executed by git, and this stops the local template
                # directory from installing any either.
                "-c",
                "core.hooksPath=",
                "clone",
                "--depth",
                "1",
                "--no-tags",
                url,
                tmp,
            ],
            capture_output=True,
            text=True,
            timeout=policy.clone_timeout_seconds,
            env={
                **os.environ,
                # Fail fast on a private/nonexistent repo instead of blocking
                # the worker on an interactive credential prompt.
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["clone failed"]
            raise ValueError(f"could not clone {url}: {detail[0]}")
        # The clone lives in our own temp dir, so the allow-list does not apply
        # to it — the user never named this path.
        return _ingest_dir(Path(tmp), language, policy, enforce_root=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"cloning {url} timed out") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ingest_dir(
    root: Path,
    language: Language,
    policy: IngestPolicy = DEFAULT_POLICY,
    *,
    enforce_root: bool = True,
) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    extensions = _EXTENSIONS.get(language, ())
    if not extensions:
        raise ValueError(f"no known file extensions for {language.value}")

    allowed_root = policy.resolved_root() if enforce_root else None
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if allowed_root is not None and not _within(path, allowed_root):
            continue  # a symlink pointing out of the allow-list
        try:
            if path.stat().st_size > policy.max_file_bytes:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary — skip rather than fail the batch
        files.append({"path": os.path.relpath(path, root), "content": content})
        if len(files) >= policy.max_files:
            break

    if not files:
        exts = ", ".join(extensions)
        raise ValueError(
            f"no {language.value} files ({exts}) found under {root}"
        )
    return files


def _within(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root)
    except (OSError, RuntimeError):
        return False
