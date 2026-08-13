"""Application services: the seam between HTTP controllers and Track A core.

Controllers speak HTTP; the Track A core (orchestrator, repositories) speaks
domain objects. :class:`~services.translation_service.TranslationService` sits
between them, and is also where the real Groq-backed ``translate_fn`` would be
plugged in — the default is a local stub so the API runs with zero network
access, exactly like the rest of the codebase.
"""

from __future__ import annotations

from services.stub_brain import stub_translate
from services.translation_service import TranslationService

__all__ = ["TranslationService", "stub_translate"]
