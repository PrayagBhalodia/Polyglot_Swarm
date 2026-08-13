"""HTTP controllers: translate requests into service calls and back.

Controllers own request parsing and validation and response shaping; they call
:class:`~services.translation_service.TranslationService` for all behaviour and
never touch Track A repositories or the orchestrator directly.
"""

from __future__ import annotations

from controllers.agent_controller import AgentController
from controllers.health_controller import health
from controllers.job_controller import JobController

__all__ = ["AgentController", "JobController", "health"]
