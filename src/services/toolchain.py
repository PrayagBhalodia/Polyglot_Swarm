"""A verification seam backed by whatever real compilers are on the machine.

The repair loop is only as good as its oracle. ``default_verify`` gives a true
verdict for Python (``ast.parse``) but falls back to counting brackets for every
other target, which means a Go or Rust translation full of nonsense sails
through the gate and the ``repair_fn`` never has anything useful to fix.

This module closes that gap without adding a dependency: for each target
language it knows a *syntax-only* command that is almost certainly already
installed if anyone is writing that language on the box —

===========  ===================================================
python       ``ast.parse`` (in-process; no subprocess at all)
javascript   ``node --check``
typescript   ``tsc --noEmit``
go           ``gofmt -e``
rust         ``rustc --edition 2021 --emit=metadata``
php          ``php -l``
ruby         ``ruby -c``
===========  ===================================================

— writes the merged file to a temp directory, runs the command with a timeout,
and turns its diagnostics into the error list ``repair_fn`` receives. The tool's
own message ("expected ';', found '}'" with a line number) is exactly the
feedback that makes a repair attempt worth making.

**It never fails because a tool is missing.** ``shutil.which`` decides; anything
unavailable — or a command that times out or cannot be launched — degrades to
the structural check from :mod:`services.verification`. That keeps the gate
honest on a developer laptop with every toolchain installed *and* on a bare CI
container with none, which is why the tests inject a fake ``which``/runner
instead of depending on what is installed.

Every command here is a parse/type check: none of them execute the translated
program. Set ``POLYGLOT_VERIFY=basic`` to opt out of subprocesses entirely.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from core.verifier import VerifyFn
from models.enums import Language, VerifyMode
from services.verification import default_verify, verify_python, verify_structure

_logger = logging.getLogger("polyglot.verify")

# Injection seams, so the dispatch logic is testable with no tools installed.
# ``Which`` resolves an executable name to a path (or ``None``); ``Runner`` runs
# a command and returns its exit code plus whatever it said about the code.
Which = Callable[[str], str | None]
Runner = Callable[[Sequence[str], float], tuple[int, str]]

_DEFAULT_TIMEOUT_SECONDS = 30.0
# Diagnostics are prompt input for the repair agent, not a build log.
_MAX_DIAGNOSTIC_LINES = 12


@dataclass(frozen=True, slots=True)
class ToolCheck:
    """A syntax-only command for one language.

    ``args`` is a template: ``{file}`` is replaced with the path of the written
    source file and ``{out}`` with a scratch output path inside the same
    (deleted) temp directory.
    """

    executable: str
    args: tuple[str, ...]
    suffix: str

    def command(self, source: Path, out: Path) -> list[str]:
        replacements = {"{file}": str(source), "{out}": str(out)}
        return [self.executable, *(replacements.get(a, a) for a in self.args)]


_TOOLCHAINS: dict[Language, ToolCheck] = {
    Language.JAVASCRIPT: ToolCheck("node", ("--check", "{file}"), ".js"),
    Language.TYPESCRIPT: ToolCheck("tsc", ("--noEmit", "{file}"), ".ts"),
    Language.GO: ToolCheck("gofmt", ("-e", "{file}"), ".go"),
    Language.RUST: ToolCheck(
        "rustc",
        (
            "--edition",
            "2021",
            "--crate-type",
            "lib",
            "--emit=metadata",
            "-o",
            "{out}",
            "{file}",
        ),
        ".rs",
    ),
    Language.PHP: ToolCheck("php", ("-l", "{file}"), ".php"),
    Language.RUBY: ToolCheck("ruby", ("-c", "{file}"), ".rb"),
}


def available_tools(which: Which = shutil.which) -> dict[Language, str]:
    """Which of the known checkers are actually installed (for startup logs)."""
    found: dict[Language, str] = {}
    for language, check in _TOOLCHAINS.items():
        if which(check.executable) is not None:
            found[language] = check.executable
    return found


def build_toolchain_verify(
    *,
    which: Which = shutil.which,
    runner: Runner | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> VerifyFn:
    """Build a ``verify_fn`` that shells out when it can and degrades when it can't."""
    run = runner or _subprocess_runner

    def verify(content: str, language: Language) -> tuple[bool, list[str]]:
        if not content.strip():
            return False, ["merged output is empty"]
        if language == Language.PYTHON:
            return verify_python(content)  # a real parser, already in-process

        check = _TOOLCHAINS.get(language)
        if check is None or which(check.executable) is None:
            return verify_structure(content)

        try:
            return _run_check(check, content, run, timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            # A missing interpreter, a hung compiler, a full disk: none of
            # these are a verdict on the translated code.
            _logger.warning(
                "%s check unavailable (%s); falling back to the structural check",
                check.executable,
                exc,
            )
            return verify_structure(content)

    return verify


def toolchain_verify(content: str, language: Language) -> tuple[bool, list[str]]:
    """The ready-made toolchain-backed ``verify_fn`` (module-level default)."""
    return _DEFAULT_VERIFY(content, language)


def verify_fn_for(settings: Settings) -> VerifyFn:
    """The ``verify_fn`` ``POLYGLOT_VERIFY`` asks for."""
    if settings.verify_mode == VerifyMode.BASIC:
        return default_verify
    return toolchain_verify


# --- Internals --------------------------------------------------------------


def _run_check(
    check: ToolCheck, content: str, run: Runner, timeout_seconds: float
) -> tuple[bool, list[str]]:
    """Write ``content`` to a scratch file, check it, and clean up after."""
    tmp = Path(tempfile.mkdtemp(prefix="polyglot-verify-"))
    try:
        source = tmp / f"polyglot_check{check.suffix}"
        source.write_text(content, encoding="utf-8")
        code, output = run(check.command(source, tmp / "out"), timeout_seconds)
        if code == 0:
            return True, []
        return False, _diagnostics(output, source) or [
            f"{check.executable} rejected the file (exit {code})"
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _diagnostics(output: str, source: Path) -> list[str]:
    """Turn tool output into short, path-free lines a repair prompt can use."""
    lines: list[str] = []
    for raw in output.splitlines():
        line = raw.strip().replace(str(source), source.name)
        if line:
            lines.append(line)
        if len(lines) >= _MAX_DIAGNOSTIC_LINES:
            lines.append("... (further diagnostics omitted)")
            break
    return lines


def _subprocess_runner(
    command: Sequence[str], timeout_seconds: float
) -> tuple[int, str]:
    """Run ``command``, returning its exit code and everything it printed.

    stderr comes first because that is where syntax errors land for most of
    these tools; ``tsc`` reports on stdout, so both are captured.
    """
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return completed.returncode, f"{completed.stderr}\n{completed.stdout}".strip()


_DEFAULT_VERIFY: VerifyFn = build_toolchain_verify()
