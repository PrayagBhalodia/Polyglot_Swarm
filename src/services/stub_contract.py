"""The default, offline contract-extraction seam.

The Orchestrator's ``extract_contract_fn`` reads the whole codebase once, before
anything is translated, and returns the shared symbol table every chapter is
then translated against. In production that is a Groq call; this is the
deterministic stand-in that lets the phase run — and be tested — with no network
and no API key, exactly like the other three seams.

It is not a parser for eighteen languages, and it does not pretend to be. It
does two honest things:

1. **Find the public symbols.** Python source is read with ``ast`` (a real
   parse); everything else is scanned for top-level declarations with a small
   set of per-family regexes. Anything it misses simply is not in the contract.
2. **Decide the target name once.** The identifier is split into words and
   re-cased to the target language's convention — ``calculate_net_pay`` becomes
   ``calculateNetPay`` in Go and Java, ``CalcNetPay``-style types become
   ``calc_net_pay`` in Python and Rust. The *value* here is not the beauty of
   the mapping but that all agents receive the **same** mapping, so chapter 3
   and chapter 7 cannot invent two different names for one function.

Deterministic by construction: same files in, same contract out, ordered by
source path and then by name.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from models.contract import Contract, ContractSymbol
from models.enums import Language
from models.source import SourceFile

# Bound the table so an enormous repository cannot produce a contract too large
# to fit in any prompt; the render step truncates again per call.
_MAX_SYMBOLS = 400

# Top-level declarations, per language family. Each pattern captures the name in
# group ``name`` and is matched against a *line* that starts at column 0 (or, for
# fixed-form legacy languages, close to it).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("function", re.compile(r"^\s{0,7}(?:async\s+)?def\s+(?P<name>\w+)\s*\(")),
    ("class", re.compile(r"^\s{0,7}class\s+(?P<name>\w+)")),
    ("function", re.compile(r"^\s{0,7}func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\(")),
    (
        "function",
        re.compile(r"^\s{0,7}(?:pub\s+)?(?:async\s+)?fn\s+(?P<name>\w+)\s*[(<]"),
    ),
    (
        "function",
        re.compile(
            r"^\s{0,7}(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\("
        ),
    ),
    ("type", re.compile(r"^\s{0,7}(?:pub\s+)?(?:struct|enum|trait)\s+(?P<name>\w+)")),
    (
        "type",
        re.compile(r"^\s{0,7}(?:export\s+)?(?:interface|type)\s+(?P<name>\w+)"),
    ),
    (
        "class",
        re.compile(
            r"^\s{0,7}(?:public\s+|private\s+|abstract\s+|final\s+|static\s+)*"
            r"class\s+(?P<name>\w+)"
        ),
    ),
    ("function", re.compile(r"^\s{0,7}(?:Public\s+|Private\s+)?Sub\s+(?P<name>\w+)")),
    (
        "function",
        re.compile(r"^\s{0,7}(?:Public\s+|Private\s+)?Function\s+(?P<name>\w+)"),
    ),
    ("function", re.compile(r"^\s{0,7}(?:procedure|function)\s+(?P<name>\w+)", re.I)),
    ("module", re.compile(r"^\s{0,7}PROGRAM-ID\.\s*(?P<name>[\w-]+)", re.I)),
    ("module", re.compile(r"^\s{0,6}(?:program|subroutine)\s+(?P<name>\w+)", re.I)),
)

# Target languages that name callables in snake_case; the rest use camelCase.
_SNAKE_CASE_TARGETS = frozenset(
    {Language.PYTHON, Language.RUST, Language.RUBY, Language.C, Language.PERL}
)
# Target languages whose types/classes are PascalCase (nearly all of them).
_LOWER_TYPE_TARGETS = frozenset({Language.C, Language.PERL})

_WORD_SPLIT = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])")


def stub_extract_contract(
    source_files: Sequence[SourceFile],
    source_language: Language,
    target_language: Language,
) -> Contract:
    """Build the shared symbol table for a job (no network, fully deterministic)."""
    symbols: list[ContractSymbol] = []
    seen: set[str] = set()

    for source_file in sorted(source_files, key=lambda f: f.path):
        for kind, name in _scan(source_file, source_language):
            if name in seen:
                continue  # first declaration wins, so the table stays stable
            seen.add(name)
            target_name = _rename(name, kind, target_language)
            symbols.append(
                ContractSymbol(
                    source_name=name,
                    target_name=target_name,
                    kind=kind,
                    signature=_signature(target_name, kind, target_language),
                    source_path=source_file.path,
                )
            )
            if len(symbols) >= _MAX_SYMBOLS:
                break
        if len(symbols) >= _MAX_SYMBOLS:
            break

    return Contract(
        source_language=source_language,
        target_language=target_language,
        symbols=tuple(symbols),
        conventions=_conventions(target_language),
    )


# --- Finding symbols --------------------------------------------------------


def scan_declarations(content: str, language: Language) -> list[tuple[str, str]]:
    """``(kind, name)`` for every top-level declaration in ``content``.

    Public because the cross-file reconciliation pass needs the same reading of
    a file — there, applied to the *translated* output, to see which symbols
    each file actually ended up defining.
    """
    if language == Language.PYTHON:
        parsed = _scan_python(content)
        if parsed is not None:
            return parsed  # a real parse beats any regex
    return _scan_lines(content)


def _scan(source_file: SourceFile, language: Language) -> list[tuple[str, str]]:
    """``(kind, name)`` for every top-level declaration in one file."""
    return scan_declarations(source_file.content, language)


def _scan_python(content: str) -> list[tuple[str, str]] | None:
    """Use the real parser for Python; ``None`` if the file does not parse."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                found.append(("function", node.name))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                found.append(("class", node.name))
    return found


def _scan_lines(content: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        for kind, pattern in _PATTERNS:
            match = pattern.match(line)
            if match is not None:
                name = match.group("name")
                if not name.startswith("_"):
                    found.append((kind, name))
                break
    return found


# --- Deciding the target name ----------------------------------------------


def _words(name: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT.split(name) if w]


def _rename(name: str, kind: str, target: Language) -> str:
    words = _words(name)
    if not words:  # pragma: no cover - the scanners never yield empty names
        return name
    if kind in ("class", "type"):
        if target in _LOWER_TYPE_TARGETS:
            return "_".join(words)
        return "".join(w.capitalize() for w in words)
    if target in _SNAKE_CASE_TARGETS:
        return "_".join(words)
    return words[0] + "".join(w.capitalize() for w in words[1:])


def _signature(target_name: str, kind: str, target: Language) -> str:
    if kind in ("class", "type", "module"):
        return target_name
    if target == Language.PYTHON:
        return f"def {target_name}(...)"
    if target == Language.GO:
        return f"func {target_name}(...)"
    if target == Language.RUST:
        return f"fn {target_name}(...)"
    if target in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        return f"function {target_name}(...)"
    return f"{target_name}(...)"


def _conventions(target: Language) -> tuple[str, ...]:
    callables = (
        "snake_case" if target in _SNAKE_CASE_TARGETS else "camelCase"
    )
    types = "snake_case" if target in _LOWER_TYPE_TARGETS else "PascalCase"
    return (
        f"Name functions in {callables} and types/classes in {types}.",
        "Use exactly the target names in the table below — never invent a "
        "second spelling for a symbol that already appears there.",
        "Emit each symbol's definition only in the chapter where its source "
        "declaration appears; elsewhere, call it.",
    )
