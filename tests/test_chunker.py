"""Tests for the source chunker.

The line-based cases below double as the structural strategy's regression net:
their fixtures have no structure to find, so a structure-aware cut degrades to
exactly the same fixed-size split.
"""

from __future__ import annotations

import unittest

from core.chunker import Chunker
from core.errors import ChunkingError
from models.enums import ChunkStrategy, Language
from models.source import SourceFile


def _file(content: str, language: Language = Language.COBOL) -> SourceFile:
    return SourceFile(path="legacy.cob", language=language, content=content)


def _chunk(
    content: str,
    *,
    language: Language = Language.PYTHON,
    target: Language = Language.RUST,
    max_lines: int = 10,
    strategy: ChunkStrategy = ChunkStrategy.STRUCTURAL,
) -> list[str]:
    """Chunk a snippet and return just the unit bodies."""
    units = Chunker(max_lines_per_unit=max_lines, strategy=strategy).chunk_file(
        SourceFile(path="f.src", language=language, content=content),
        job_id="j",
        target_language=target,
    )
    return [u.content for u in units]


class ChunkerConfigTests(unittest.TestCase):
    def test_rejects_bad_config(self) -> None:
        with self.assertRaises(ValueError):
            Chunker(max_lines_per_unit=0)
        with self.assertRaises(ValueError):
            Chunker(max_lines_per_unit=5, overlap_lines=5)

    def test_defaults_to_the_structural_strategy(self) -> None:
        self.assertEqual(Chunker().strategy, ChunkStrategy.STRUCTURAL)


class ChunkFileTests(unittest.TestCase):
    def test_splits_by_max_lines_and_covers_all_lines(self) -> None:
        content = "".join(f"line {i}\n" for i in range(10))
        units = Chunker(max_lines_per_unit=4).chunk_file(
            _file(content), job_id="j", target_language=Language.PYTHON
        )
        # 10 lines / 4 per unit -> 3 units (4, 4, 2)
        self.assertEqual([u.index for u in units], [0, 1, 2])
        self.assertEqual(units[0].start_line, 1)
        self.assertEqual(units[0].end_line, 4)
        self.assertEqual(units[-1].end_line, 10)
        # Concatenating unit bodies reproduces the original file exactly.
        self.assertEqual("".join(u.content for u in units), content)

    def test_single_unit_for_small_file(self) -> None:
        units = Chunker(max_lines_per_unit=100).chunk_file(
            _file("a\nb\n"), job_id="j", target_language=Language.PYTHON
        )
        self.assertEqual(len(units), 1)

    def test_overlap_repeats_context(self) -> None:
        content = "".join(f"{i}\n" for i in range(6))
        units = Chunker(max_lines_per_unit=4, overlap_lines=1).chunk_file(
            _file(content), job_id="j", target_language=Language.PYTHON
        )
        # step = 3; units start at line 1 and line 4.
        self.assertEqual(units[0].start_line, 1)
        self.assertEqual(units[1].start_line, 4)

    def test_empty_file_rejected(self) -> None:
        with self.assertRaises(ChunkingError):
            Chunker().chunk_file(
                _file(""), job_id="j", target_language=Language.PYTHON
            )

    def test_same_target_language_rejected(self) -> None:
        with self.assertRaises(ChunkingError):
            Chunker().chunk_file(
                _file("x\n"), job_id="j", target_language=Language.COBOL
            )


_PY_SOURCE = """\
import os


def alpha():
    a = 1
    b = 2
    return a + b


def beta():
    return "beta"


class Gamma:
    def method(self):
        return 3
"""

_GO_SOURCE = """\
package main

import "fmt"

func alpha() int {
	x := 1
	y := 2
	return x + y
}

func beta() string {
	return "beta"
}
"""


class StructuralChunkingTests(unittest.TestCase):
    def test_a_function_is_not_split_across_units(self) -> None:
        # A budget of 6 lines would cut straight through alpha() under the
        # line strategy; the structural cut pulls back to the blank line
        # before "def beta".
        units = _chunk(_PY_SOURCE, max_lines=6)
        self.assertGreater(len(units), 1)
        holder = [u for u in units if "def alpha" in u]
        self.assertEqual(len(holder), 1)
        self.assertIn("return a + b", holder[0])

    def test_line_strategy_still_slices_mid_function(self) -> None:
        units = _chunk(_PY_SOURCE, max_lines=6, strategy=ChunkStrategy.LINES)
        holder = [u for u in units if "def alpha" in u][0]
        # Proof the structural mode is doing something: the naive split leaves
        # alpha()'s body dangling into the next unit.
        self.assertNotIn("return a + b", holder)

    def test_brace_language_boundaries_are_respected(self) -> None:
        units = _chunk(_GO_SOURCE, language=Language.GO, target=Language.PYTHON,
                       max_lines=8)
        holder = [u for u in units if "func alpha" in u]
        self.assertEqual(len(holder), 1)
        self.assertIn("return x + y", holder[0])

    def test_cobol_divisions_start_new_units(self) -> None:
        source = (
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID. PAYROLL.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            "       01 WS-COUNTER PIC 9(4).\n"
            "       PROCEDURE DIVISION.\n"
            "           MOVE 1 TO WS-COUNTER.\n"
            "           STOP RUN.\n"
        )
        units = _chunk(
            source, language=Language.COBOL, target=Language.PYTHON, max_lines=4
        )
        # Every unit after the first opens on a DIVISION/SECTION header rather
        # than somewhere in the middle of a paragraph.
        self.assertGreater(len(units), 1)
        for unit in units[1:]:
            first_line = unit.splitlines()[0].strip().lower()
            self.assertTrue(
                first_line.endswith(("division.", "section.")),
                f"unit opened mid-paragraph: {first_line!r}",
            )

    def test_units_never_exceed_the_maximum(self) -> None:
        for max_lines in (1, 2, 3, 5, 8, 13):
            units = _chunk(_PY_SOURCE, max_lines=max_lines)
            for unit in units:
                self.assertLessEqual(len(unit.splitlines()), max_lines)
                self.assertTrue(unit)  # never empty

    def test_every_line_is_covered_exactly_once(self) -> None:
        self.assertEqual("".join(_chunk(_PY_SOURCE, max_lines=5)), _PY_SOURCE)

    def test_chunking_is_deterministic(self) -> None:
        first = _chunk(_PY_SOURCE, max_lines=6)
        second = _chunk(_PY_SOURCE, max_lines=6)
        self.assertEqual(first, second)

    def test_unstructured_content_degrades_to_the_line_split(self) -> None:
        content = "".join(f"    payload {i}\n" for i in range(9))
        self.assertEqual(
            _chunk(content, max_lines=4),
            _chunk(content, max_lines=4, strategy=ChunkStrategy.LINES),
        )

    def test_whitespace_only_file_yields_one_unit(self) -> None:
        units = _chunk("   \n\n\t\n", max_lines=10)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0], "   \n\n\t\n")

    def test_one_giant_construct_is_split_at_the_bound(self) -> None:
        body = "".join(f"    step_{i}()\n" for i in range(20))
        units = _chunk(f"def huge():\n{body}", max_lines=5)
        self.assertGreater(len(units), 1)
        for unit in units:
            self.assertLessEqual(len(unit.splitlines()), 5)

    def test_decorators_stay_with_their_function(self) -> None:
        source = (
            "def first():\n    return 1\n"
            "@decorated\n"
            "def second():\n    return 2\n"
        )
        units = _chunk(source, max_lines=3)
        holder = [u for u in units if "@decorated" in u][0]
        self.assertIn("def second", holder)


class ChunkFilesTests(unittest.TestCase):
    def test_global_index_is_contiguous(self) -> None:
        f1 = _file("a\nb\nc\n")
        f2 = _file("d\ne\nf\n")
        units = Chunker(max_lines_per_unit=2).chunk_files(
            [f1, f2], job_id="j", target_language=Language.PYTHON
        )
        self.assertEqual([u.index for u in units], list(range(len(units))))
        # Two source files represented.
        self.assertEqual(len({u.source_file_id for u in units}), 2)


if __name__ == "__main__":
    unittest.main()
