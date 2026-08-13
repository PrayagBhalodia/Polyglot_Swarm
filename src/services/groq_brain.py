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

import time

from config.settings import Settings
from core.merger import MergeFn
from core.orchestrator import TranslateFn
from core.verifier import RepairFn
from models.agent import SwarmAgent
from models.merge import MergeResult, MergeTask
from models.result import TranslationResult
from models.source import TranslationUnit
from models.verification import RepairRequest
from services.groq_client import CompletionClient, GroqClient


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
        start = time.perf_counter()
        try:
            completion = client.complete(
                system=system, user=unit.content, model=agent.model
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
        user = f"LEFT:\n{task.left}\n\nRIGHT:\n{task.right}"
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


def make_groq_seams(
    settings: Settings, *, client: CompletionClient | None = None
) -> tuple[TranslateFn, MergeFn, RepairFn]:
    """Build all three Groq-backed seams from settings (or an injected client)."""
    client = client or GroqClient(settings.groq)
    return (
        build_translate_fn(client),
        build_merge_fn(client),
        build_repair_fn(client),
    )


def maybe_groq_seams(
    settings: Settings,
) -> tuple[TranslateFn | None, MergeFn | None, RepairFn | None]:
    """Groq seams when an API key is configured, else ``None`` (offline stubs)."""
    if not settings.groq.api_key:
        return None, None, None
    return make_groq_seams(settings)


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
