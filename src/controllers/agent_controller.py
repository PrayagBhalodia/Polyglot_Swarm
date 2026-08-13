"""Read-only endpoints describing the configured swarm."""

from __future__ import annotations

from api.http import Request, Response
from services.translation_service import TranslationService


class AgentController:
    def __init__(self, service: TranslationService) -> None:
        self._service = service

    def list(self, request: Request) -> Response:
        """``GET /agents`` → the swarm as configured by settings."""
        agents = self._service.agents()
        return Response(
            200,
            {"agents": [a.to_dict() for a in agents], "count": len(agents)},
        )
