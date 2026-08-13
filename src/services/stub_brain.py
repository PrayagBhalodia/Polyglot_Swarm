"""The default, offline translation seam for the API.

Track A exposes ``translate_fn(unit, agent) -> TranslationResult`` as the point
where the Groq-powered Brain plugs in. Track B needs *some* implementation to
run jobs end-to-end in tests and demos without a network or API key, so it ships
this deterministic stub. A production deployment injects a real Groq client into
:class:`~services.translation_service.TranslationService` in its place; nothing
else in the API layer changes.
"""

from __future__ import annotations

from models.agent import SwarmAgent
from models.result import TranslationResult
from models.source import TranslationUnit


def stub_translate(unit: TranslationUnit, agent: SwarmAgent) -> TranslationResult:
    """Emit a labelled placeholder translation for one unit (no network)."""
    comments = [f"    # {line.strip()}" for line in unit.content.splitlines() if line.strip()]
    # Always end the body with a real statement so the placeholder is valid
    # Python and clears the verification gate.
    body = "\n".join([*comments, "    pass"])
    translated = (
        f"# --- chapter {unit.index} (translated by {agent.name}) ---\n"
        f"def chapter_{unit.index}():\n{body}\n"
    )
    return TranslationResult(
        unit_id=unit.id,
        target_language=unit.target_language,
        translated_content=translated,
        agent_id=agent.id,
        tokens_used=len(unit.content),
        duration_ms=1,
    )
