"""Tests for the toolchain-backed verification seam.

The dispatch, fallback, and diagnostic-shaping logic is tested with an injected
fake ``which``/``runner``, so these run identically on a laptop with every
compiler installed and on a bare container with none. A second, small class
exercises the *real* commands, and every one of its tests is skipped unless
``shutil.which`` finds the tool — the suite must never depend on what happens to
be installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from collections.abc import Sequence
from unittest import mock

from config.settings import load_settings
from models.enums import Language, VerifyMode
from services.toolchain import (
    available_tools,
    build_toolchain_verify,
    toolchain_verify,
    verify_fn_for,
)
from services.verification import default_verify


class _FakeRunner:
    """Records the commands it was asked to run and replays a canned result."""

    def __init__(self, code: int = 0, output: str = "") -> None:
        self.code = code
        self.output = output
        self.commands: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], timeout_seconds: float
    ) -> tuple[int, str]:
        self.commands.append(list(command))
        return self.code, self.output


def _which_only(*available: str):  # type: ignore[no-untyped-def]
    """A ``which`` that finds exactly the named executables."""

    def which(executable: str) -> str | None:
        return f"/usr/bin/{executable}" if executable in available else None

    return which


class DispatchTests(unittest.TestCase):
    def test_python_never_shells_out(self) -> None:
        runner = _FakeRunner()
        verify = build_toolchain_verify(which=_which_only("node"), runner=runner)
        ok, errors = verify("def f():\n    pass\n", Language.PYTHON)
        self.assertTrue(ok)
        self.assertEqual(errors, [])
        self.assertEqual(runner.commands, [])

    def test_python_syntax_error_is_reported(self) -> None:
        verify = build_toolchain_verify(
            which=_which_only(), runner=_FakeRunner()
        )
        ok, errors = verify("def f(:\n    pass\n", Language.PYTHON)
        self.assertFalse(ok)
        self.assertTrue(errors and "line" in errors[0])

    def test_available_tool_is_invoked(self) -> None:
        runner = _FakeRunner(code=0)
        verify = build_toolchain_verify(which=_which_only("node"), runner=runner)
        ok, errors = verify("const x = 1;\n", Language.JAVASCRIPT)

        self.assertTrue(ok)
        self.assertEqual(errors, [])
        self.assertEqual(len(runner.commands), 1)
        command = runner.commands[0]
        self.assertEqual(command[0], "node")
        self.assertIn("--check", command)
        self.assertTrue(command[-1].endswith(".js"))

    def test_nonzero_exit_becomes_diagnostics(self) -> None:
        runner = _FakeRunner(
            code=1, output="polyglot_check.js:2\nSyntaxError: Unexpected token\n"
        )
        verify = build_toolchain_verify(which=_which_only("node"), runner=runner)
        ok, errors = verify("const = ;\n", Language.JAVASCRIPT)

        self.assertFalse(ok)
        self.assertIn("SyntaxError: Unexpected token", errors)

    def test_nonzero_exit_without_output_still_explains_itself(self) -> None:
        runner = _FakeRunner(code=2, output="   \n")
        verify = build_toolchain_verify(which=_which_only("gofmt"), runner=runner)
        ok, errors = verify("package main\n", Language.GO)
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertIn("gofmt", errors[0])

    def test_diagnostics_are_capped(self) -> None:
        runner = _FakeRunner(code=1, output="\n".join(f"error {i}" for i in range(50)))
        verify = build_toolchain_verify(which=_which_only("ruby"), runner=runner)
        _, errors = verify("def x", Language.RUBY)
        self.assertLessEqual(len(errors), 13)
        self.assertIn("omitted", errors[-1])

    def test_missing_tool_falls_back_to_structural(self) -> None:
        runner = _FakeRunner()
        verify = build_toolchain_verify(which=_which_only(), runner=runner)

        ok, _ = verify("func main() { return }\n", Language.GO)
        self.assertTrue(ok)
        bad_ok, bad_errors = verify("func main() {\n", Language.GO)
        self.assertFalse(bad_ok)
        self.assertTrue(bad_errors)
        self.assertEqual(runner.commands, [])  # nothing was ever launched

    def test_language_without_a_known_tool_falls_back(self) -> None:
        runner = _FakeRunner()
        verify = build_toolchain_verify(
            which=_which_only("node", "gofmt"), runner=runner
        )
        ok, _ = verify("IDENTIFICATION DIVISION.\n", Language.COBOL)
        self.assertTrue(ok)
        self.assertEqual(runner.commands, [])

    def test_empty_output_is_rejected_before_any_tool(self) -> None:
        runner = _FakeRunner()
        verify = build_toolchain_verify(which=_which_only("node"), runner=runner)
        ok, errors = verify("   \n", Language.JAVASCRIPT)
        self.assertFalse(ok)
        self.assertEqual(errors, ["merged output is empty"])
        self.assertEqual(runner.commands, [])


class ToolFailureTests(unittest.TestCase):
    """A broken tool is not a verdict on the code — it must degrade, not raise."""

    def _verify_with(self, exc: Exception):  # type: ignore[no-untyped-def]
        def runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
            raise exc

        return build_toolchain_verify(which=_which_only("gofmt"), runner=runner)

    def test_timeout_degrades_to_structural(self) -> None:
        verify = self._verify_with(subprocess.TimeoutExpired("gofmt", 30))
        with self.assertLogs("polyglot.verify", level="WARNING"):
            ok, _ = verify("func main() { }\n", Language.GO)
        self.assertTrue(ok)

    def test_launch_failure_degrades_to_structural(self) -> None:
        verify = self._verify_with(OSError("Exec format error"))
        with self.assertLogs("polyglot.verify", level="WARNING"):
            ok, errors = verify("func main() {\n", Language.GO)
        self.assertFalse(ok)  # the structural check still has an opinion
        self.assertTrue(errors)

    def test_a_missing_tool_never_raises_for_any_language(self) -> None:
        verify = build_toolchain_verify(which=_which_only(), runner=_FakeRunner())
        for language in Language:
            if language == Language.PYTHON:
                continue
            ok, errors = verify("x = 1\n", language)
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(errors, list)


class TempFileHygieneTests(unittest.TestCase):
    def test_scratch_directory_is_removed(self) -> None:
        seen: list[str] = []

        def runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
            seen.append(command[-1])
            self.assertTrue(os.path.exists(command[-1]))
            return 0, ""

        verify = build_toolchain_verify(which=_which_only("node"), runner=runner)
        verify("const x = 1;\n", Language.JAVASCRIPT)

        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))
        self.assertFalse(os.path.exists(os.path.dirname(seen[0])))


class SelectionTests(unittest.TestCase):
    def test_basic_mode_selects_the_stdlib_gate(self) -> None:
        with mock.patch.dict(os.environ, {"POLYGLOT_VERIFY": "basic"}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.verify_mode, VerifyMode.BASIC)
        self.assertIs(verify_fn_for(settings), default_verify)

    def test_toolchain_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.verify_mode, VerifyMode.TOOLCHAIN)
        self.assertIs(verify_fn_for(settings), toolchain_verify)

    def test_available_tools_reports_what_which_finds(self) -> None:
        found = available_tools(_which_only("node", "ruby", "cargo"))
        self.assertEqual(
            set(found), {Language.JAVASCRIPT, Language.RUBY}
        )


class RealToolchainTests(unittest.TestCase):
    """Exercises the actual commands — every case skips if the tool is absent."""

    def _assert_verdicts(self, language: Language, good: str, bad: str) -> None:
        ok, errors = toolchain_verify(good, language)
        self.assertTrue(ok, f"valid {language.value} was rejected: {errors}")
        self.assertEqual(errors, [])

        bad_ok, bad_errors = toolchain_verify(bad, language)
        self.assertFalse(bad_ok, f"invalid {language.value} was accepted")
        self.assertTrue(bad_errors)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node_checks_javascript(self) -> None:
        self._assert_verdicts(
            Language.JAVASCRIPT,
            "function add(a, b) {\n  return a + b;\n}\n",
            "function add(a, b {\n  return a + ;\n",
        )

    @unittest.skipUnless(shutil.which("gofmt"), "gofmt is not installed")
    def test_gofmt_checks_go(self) -> None:
        self._assert_verdicts(
            Language.GO,
            'package main\n\nfunc add(a, b int) int {\n\treturn a + b\n}\n',
            "package main\n\nfunc add(a, b int int {\n\treturn a +\n",
        )

    @unittest.skipUnless(shutil.which("ruby"), "ruby is not installed")
    def test_ruby_checks_ruby(self) -> None:
        self._assert_verdicts(
            Language.RUBY,
            "def add(a, b)\n  a + b\nend\n",
            "def add(a, b\n  a +\n",
        )

    @unittest.skipUnless(shutil.which("php"), "php is not installed")
    def test_php_checks_php(self) -> None:
        self._assert_verdicts(
            Language.PHP,
            "<?php\nfunction add($a, $b) { return $a + $b; }\n",
            "<?php\nfunction add($a, $b { return $a + ; }\n",
        )

    @unittest.skipUnless(shutil.which("rustc"), "rustc is not installed")
    def test_rustc_checks_rust(self) -> None:
        self._assert_verdicts(
            Language.RUST,
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
            "pub fn add(a: i32, b: i32 -> i32 {\n    a +\n",
        )

    @unittest.skipUnless(shutil.which("python3"), "python3 is not installed")
    def test_python_is_checked_in_process(self) -> None:
        self._assert_verdicts(
            Language.PYTHON,
            "def add(a, b):\n    return a + b\n",
            "def add(a, b:\n    return a +\n",
        )


if __name__ == "__main__":
    unittest.main()
