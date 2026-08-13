"""Wire a real Groq client into the translate / merge / repair seams.

Each factory turns a :class:`~services.groq_client.CompletionClient` into one of
the pipeline's seam functions, so the coordination core is unchanged — it still
sees ``translate_fn`` / ``merge_fn`` / ``repair_fn`` and never knows a network is
involved. A failed call becomes a *failed result* (for translate/merge) or a
no-op that leaves the verification gate to catch it (for repair), so a flaky
model degrades gracefully instead of crashing a job.

The ``verify_fn`` gate stays local (``ast.parse``); no LLM is needed to tell
whether code parses, and using a deterministic oracle is the whole point.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import NamedTuple

from config.settings import Settings
from core.merger import MergeFn
from core.orchestrator import ExtractContractFn, TranslateFn
from core.reconciler import ReconcileFn
from core.verifier import RepairFn
from models.agent import SwarmAgent
from models.contract import Contract, ContractSymbol
from models.enums import Language
from models.merge import MergeResult, MergeTask
from models.reconcile import ReconcileResult, ReconcileTask
from models.result import TranslationResult
from models.source import SourceFile, TranslationUnit
from models.verification import RepairRequest
from services.groq_client import CompletionClient, GroqClient
from services.stub_contract import stub_extract_contract

_logger = logging.getLogger("polyglot.brain")

# How much source to show the contract pass per file. The contract only needs
# declarations, which cluster near the top of a file.
_CONTRACT_HEAD_LINES = 120
_CONTRACT_MAX_FILES = 40


def build_translate_fn(client: CompletionClient) -> TranslateFn:
    """A ``translate_fn`` that translates one chapter via Groq."""

    def translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
        source = unit.source_language.value
        target = unit.target_language.value
        system = (
            f"You are an expert engineer translating {source} to idiomatic "
            f"{target}. Translate the given code fragment, preserving behaviour. "
            f"It is one chapter of a larger file, so translate only what is shown "
            f"and keep top-level structure. Output only {target} code — no prose, "
            f"no markdown fences."
        )
        # The chapter is not the whole story: say which file it came from, which
        # lines it covers, and — crucially — the contract it must agree with, so
        # this agent reaches the same names as every other agent on the job.
        where = unit.source_path or "the source file"
        user = (
            f"File: {where} (lines {unit.start_line}-{unit.end_line}, "
            f"chapter {unit.index})\n"
        )
        contract_text = _contract_context(unit.contract, unit.source_path)
        if contract_text:
            user += (
                "\nShared contract — every chapter of this codebase is being "
                "translated against it, so use these exact names and "
                f"signatures:\n{contract_text}\n"
            )
        user += f"\n{source} code to translate:\n{unit.content}"

        start = time.perf_counter()
        try:
            completion = client.complete(
                system=system, user=user, model=agent.model
            )
        except Exception as exc:  # noqa: BLE001 - surface as a failed unit
            return TranslationResult.failure(
                unit.id, unit.target_language, f"groq translate: {exc}",
                agent_id=agent.id,
            )
        return TranslationResult(
            unit_id=unit.id,
            target_language=unit.target_language,
            translated_content=_strip_code_fences(completion.text),
            agent_id=agent.id,
            tokens_used=completion.tokens,
            duration_ms=_elapsed_ms(start),
        )

    return translate


def build_merge_fn(client: CompletionClient) -> MergeFn:
    """A ``merge_fn`` that reconciles two adjacent chapters via Groq."""

    def merge(task: MergeTask, agent: SwarmAgent) -> MergeResult:
        target = task.target_language.value
        system = (
            f"You reconcile two adjacent {target} code fragments into one coherent "
            f"fragment. Keep LEFT before RIGHT; remove duplicated imports or "
            f"declarations, unify names and signatures across the seam, and change "
            f"nothing else. Output only the merged {target} code — no prose, no "
            f"markdown fences."
        )
        user = ""
        if task.source_path:
            user += f"File: {task.source_path}\n"
        contract_text = _contract_context(task.contract, task.source_path)
        if contract_text:
            user += (
                "\nShared contract — resolve any disagreement at the seam "
                f"towards these names and signatures:\n{contract_text}\n"
            )
        user += f"\nLEFT:\n{task.left}\n\nRIGHT:\n{task.right}"
        start = time.perf_counter()
        try:
            completion = client.complete(
                system=system, user=user, model=agent.model
            )
        except Exception as exc:  # noqa: BLE001 - surface as a failed merge
            return MergeResult.failure(
                task.source_file_id, task.target_language, f"groq merge: {exc}",
                agent_id=agent.id,
            )
        return MergeResult(
            source_file_id=task.source_file_id,
            target_language=task.target_language,
            merged=_strip_code_fences(completion.text),
            agent_id=agent.id,
            tokens_used=completion.tokens,
            duration_ms=_elapsed_ms(start),
        )

    return merge


def build_repair_fn(client: CompletionClient) -> RepairFn:
    """A ``repair_fn`` that fixes a file the verification gate rejected."""

    def repair(request: RepairRequest, agent: SwarmAgent) -> str:
        target = request.target_language.value
        system = (
            f"You fix {target} code that failed to parse. Return only corrected "
            f"{target} code that resolves the reported errors — no prose, no "
            f"markdown fences."
        )
        user = "Errors:\n" + "\n".join(request.errors) + "\n\nCode:\n" + request.content
        try:
            completion = client.complete(
                system=system, user=user, model=agent.model
            )
        except Exception:  # noqa: BLE001 - leave the gate to reject the unchanged file
            return request.content
        return _strip_code_fences(completion.text)

    return repair


def build_reconcile_fn(client: CompletionClient) -> ReconcileFn:
    """A ``reconcile_fn`` that aligns one finished file with the rest via Groq.

    The prompt is deliberately narrow: this pass runs on code that already
    merged cleanly, so its licence is to fix cross-file disagreement and
    nothing else. A failure hands the unchanged file back — the merged content
    is still correct, and the verification gate runs after this either way.
    """

    def reconcile(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
        target = task.target_language.value
        system = (
            f"You are aligning one file of a freshly translated {target} "
            f"codebase with its siblings. Fix ONLY cross-file inconsistencies: "
            f"names that disagree with the agreed contract, calls to symbols "
            f"another file defines under a different name, imports of things "
            f"that do not exist, and helpers duplicated from another file. "
            f"Preserve behaviour and structure, and if the file is already "
            f"consistent return it byte-for-byte unchanged. Output only the "
            f"{target} code — no prose, no markdown fences."
        )
        user = f"File: {task.source_path}\n\n{task.render_context()}\n\nCode:\n{task.content}"
        start = time.perf_counter()
        try:
            completion = client.complete(
                system=system, user=user, model=agent.model
            )
        except Exception as exc:  # noqa: BLE001 - advisory pass, never fatal
            _logger.warning(
                "reconciliation of %s failed (%s); keeping the merged file",
                task.source_path,
                exc,
            )
            return ReconcileResult.failure(
                task.source_file_id,
                task.target_language,
                task.content,
                f"groq reconcile: {exc}",
                agent_id=agent.id,
            )
        return ReconcileResult(
            source_file_id=task.source_file_id,
            target_language=task.target_language,
            content=_strip_code_fences(completion.text),
            agent_id=agent.id,
            tokens_used=completion.tokens,
            duration_ms=_elapsed_ms(start),
        )

    return reconcile


def build_extract_contract_fn(client: CompletionClient) -> ExtractContractFn:
    """An ``extract_contract_fn`` that agrees the shared symbol table via Groq.

    This is the one call that sees the *whole* codebase, and it runs before any
    chapter is translated — so what it decides is what every agent afterwards
    has to live with. A malformed or failed response falls back to the
    deterministic stub rather than dropping the contract: a mediocre shared
    vocabulary still beats eighteen private ones.
    """

    def extract(
        source_files: Sequence[SourceFile],
        source_language: Language,
        target_language: Language,
    ) -> Contract:
        if not source_files:
            return Contract(
                source_language=source_language, target_language=target_language
            )
        system = (
            f"You are planning a {source_language.value} to "
            f"{target_language.value} port. Read the code and produce the "
            f"shared API contract every translator must follow. Reply with JSON "
            f"only: {{\"conventions\": [\"...\"], \"symbols\": [{{\"source_name\": "
            f"\"...\", \"target_name\": \"...\", \"kind\": "
            f"\"function|class|type|constant|module\", \"signature\": \"...\", "
            f"\"source_path\": \"...\"}}]}}. Include public functions, types, and "
            f"classes only. target_name must be idiomatic {target_language.value} "
            f"and each source_name must appear exactly once."
        )
        try:
            completion = client.complete(
                system=system, user=_contract_digest(source_files)
            )
            return _parse_contract(
                completion.text, source_language, target_language
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the job
            _logger.warning(
                "contract extraction failed (%s); using the offline stub", exc
            )
            return stub_extract_contract(
                source_files, source_language, target_language
            )

    return extract


class GroqSeams(NamedTuple):
    """Every seam the Brain track supplies, in one bundle."""

    translate_fn: TranslateFn | None = None
    merge_fn: MergeFn | None = None
    repair_fn: RepairFn | None = None
    extract_contract_fn: ExtractContractFn | None = None
    reconcile_fn: ReconcileFn | None = None


def make_groq_seams(
    settings: Settings, *, client: CompletionClient | None = None
) -> GroqSeams:
    """Build every Groq-backed seam from settings (or an injected client)."""
    client = client or GroqClient(settings.groq)
    return GroqSeams(
        translate_fn=build_translate_fn(client),
        merge_fn=build_merge_fn(client),
        repair_fn=build_repair_fn(client),
        extract_contract_fn=build_extract_contract_fn(client),
        reconcile_fn=build_reconcile_fn(client),
    )


def maybe_groq_seams(settings: Settings) -> GroqSeams:
    """Groq seams when an API key is configured, else all ``None`` (offline stubs)."""
    if not settings.groq.api_key:
        return GroqSeams()
    return make_groq_seams(settings)


# --- Contract helpers -------------------------------------------------------


def _contract_context(contract: Contract | None, source_path: str) -> str:
    if contract is None or contract.is_empty:
        return ""
    return contract.render(focus_path=source_path)


def _contract_digest(source_files: Sequence[SourceFile]) -> str:
    """The heads of each file — enough to see declarations, not whole bodies."""
    parts: list[str] = []
    for source_file in list(source_files)[:_CONTRACT_MAX_FILES]:
        head = source_file.content.splitlines()[:_CONTRACT_HEAD_LINES]
        parts.append(f"--- {source_file.path} ---\n" + "\n".join(head))
    if len(source_files) > _CONTRACT_MAX_FILES:
        parts.append(f"... and {len(source_files) - _CONTRACT_MAX_FILES} more files")
    return "\n\n".join(parts)


def _parse_contract(
    text: str, source_language: Language, target_language: Language
) -> Contract:
    """Turn the model's JSON into a :class:`Contract`, skipping junk entries."""
    data = json.loads(_strip_code_fences(text))
    if not isinstance(data, dict):
        raise ValueError("contract response was not a JSON object")

    symbols: list[ContractSymbol] = []
    seen: set[str] = set()
    for raw in data.get("symbols", []):
        if not isinstance(raw, dict):
            continue
        source_name = str(raw.get("source_name", "")).strip()
        target_name = str(raw.get("target_name", "")).strip()
        if not source_name or not target_name or source_name in seen:
            continue
        seen.add(source_name)
        symbols.append(
            ContractSymbol(
                source_name=source_name,
                target_name=target_name,
                kind=str(raw.get("kind", "function")).strip() or "function",
                signature=str(raw.get("signature", "")).strip(),
                source_path=str(raw.get("source_path", "")).strip(),
            )
        )

    conventions = tuple(
        str(c).strip()
        for c in data.get("conventions", [])
        if isinstance(c, (str, int, float)) and str(c).strip()
    )
    return Contract(
        source_language=source_language,
        target_language=target_language,
        symbols=tuple(symbols),
        conventions=conventions,
    )


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _strip_code_fences(text: str) -> str:
    """Drop a leading/trailing ```lang fence the model may have added anyway."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text.strip()
    lines = stripped.splitlines()
    lines = lines[1:]  # drop the opening ``` / ```python line
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")
