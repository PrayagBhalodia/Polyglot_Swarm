"""The declarative route table: URLs → controller methods.

This is the one place to read to know the full public API surface. It takes
already-constructed controllers so wiring (which service, which DB) stays in
:func:`api.app.build_app`.
"""

from __future__ import annotations

from controllers.agent_controller import AgentController
from controllers.health_controller import health
from controllers.job_controller import JobController
from controllers.ui_controller import index
from routes.router import Router


def build_router(*, jobs: JobController, agents: AgentController) -> Router:
    router = Router()

    router.add("GET", "/", index)
    router.add("GET", "/ui", index)
    router.add("GET", "/health", health)

    router.add("GET", "/jobs", jobs.list)
    router.add("POST", "/jobs", jobs.create)
    router.add("POST", "/jobs/ingest", jobs.ingest)
    router.add("GET", "/jobs/{id}", jobs.get)
    router.add("POST", "/jobs/{id}/run", jobs.run)
    router.add("GET", "/jobs/{id}/units", jobs.units)
    router.add("GET", "/jobs/{id}/results", jobs.results)

    router.add("GET", "/agents", agents.list)

    return router
