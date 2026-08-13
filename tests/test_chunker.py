"""Tests for the source chunker."""

from __future__ import annotations

import unittest

from core.chunker import Chunker
from core.errors import ChunkingError
from models.enums import Language
from models.source import SourceFile


def _file(content: str, language: Language = Language.COBOL) -> SourceFile:
    return SourceFile(path="legacy.cob", language=language, content=content)


class ChunkerConfigTests(unittest.TestCase):
    def test_rejects_bad_config(self) -> None:
        with self.assertRaises(ValueError):
            Chunker(max_lines_per_unit=0)
        with self.assertRaises(ValueError):
            Chunker(max_lines_per_unit=5, overlap_lines=5)


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
