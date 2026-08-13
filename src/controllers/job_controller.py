"""Controller for the job lifecycle endpoints.

Routes served (all JSON):

* ``POST /jobs``              create a job (inline source files)
* ``POST /jobs/ingest``       create a job from a local folder or GitHub repo
* ``GET  /jobs``             list jobs (summaries)
* ``GET  /jobs/{id}``        fetch one job (full aggregate)
* ``POST /jobs/{id}/run``    run the translate pipeline
* ``GET  /jobs/{id}/units``  the job's units
* ``GET  /jobs/{id}/results``  stored per-unit results
"""

from __future__ import annotations

from typing import Any

from api.http import Request, Response
from core.orchestrator import RunReport
from middleware.errors import BadRequestError, NotFoundError
from models.job import TranslationJob
from services.translation_service import TranslationService


class JobController:
    def __init__(self, service: TranslationService) -> None:
        self._service = service

    # --- Commands -----------------------------------------------------------

    def create(self, request: Request) -> Response:
        body = _require_object(request.json_body)
        name = _require_str(body, "name")
        source_language = _require_str(body, "source_language")
        target_language = _require_str(body, "target_language")
        source_files = _require_source_files(body)

        job = self._service.create_job(
            name=name,
            source_language=source_language,
            target_language=target_language,
            source_files=source_files,
        )
        return Response(201, job.to_dict(), {"Location": f"/jobs/{job.id}"})

    def ingest(self, request: Request) -> Response:
        body = _require_object(request.json_body)
        source_language = _require_str(body, "source_language")
        target_language = _require_str(body, "target_language")
        source_kind = _require_str(body, "source_kind")
        location = _require_str(body, "location")
        name = body.get("name") if isinstance(body.get("name"), str) else None

        try:
            job = self._service.create_job_from_source(
                name=name,
                source_language=source_language,
                target_language=target_language,
                source_kind=source_kind,
                location=location,
            )
        except ValueError as exc:
            # Bad language, unreadable location, or no matching files → 400.
            raise BadRequestError(str(exc)) from exc
        return Response(201, job.to_dict(), {"Location": f"/jobs/{job.id}"})

    def run(self, request: Request) -> Response:
        job = self._load(request)
        report = self._service.run(job)
        return Response(200, {"job": job.to_dict(), "report": _report_dict(report)})

    # --- Queries ------------------------------------------------------------

    def list(self, request: Request) -> Response:
        jobs = self._service.list_jobs()
        return Response(
            200,
            {"jobs": [_summary(job) for job in jobs], "count": len(jobs)},
        )

    def get(self, request: Request) -> Response:
        return Response(200, self._load(request).to_dict())

    def units(self, request: Request) -> Response:
        job = self._load(request)
        return Response(
            200,
            {"units": [u.to_dict() for u in job.units], "count": job.total_units},
        )

    def results(self, request: Request) -> Response:
        job = self._load(request)
        results = self._service.results_for(job)
        return Response(
            200,
            {"results": [r.to_dict() for r in results], "count": len(results)},
        )

    # --- Internals ----------------------------------------------------------

    def _load(self, request: Request) -> TranslationJob:
        job_id = request.path_params["id"]
        job = self._service.get_job(job_id)
        if job is None:
            raise NotFoundError(f"job {job_id!r} not found")
        return job


# --- Request validation helpers --------------------------------------------


def _require_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequestError("request body must be a JSON object")
    return body


def _require_str(body: dict[str, Any], field: str) -> str:
    if field not in body:
        raise BadRequestError(f"missing required field: {field!r}")
    value = body[field]
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"field {field!r} must be a non-empty string")
    return value


def _require_source_files(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("source_files")
    if not isinstance(raw, list) or not raw:
        raise BadRequestError(
            "field 'source_files' must be a non-empty array of "
            "{path, content} objects"
        )
    files: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise BadRequestError(f"source_files[{i}] must be an object")
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not path.strip():
            raise BadRequestError(f"source_files[{i}].path must be a non-empty string")
        if not isinstance(content, str):
            raise BadRequestError(f"source_files[{i}].content must be a string")
        files.append({"path": path, "content": content})
    return files


# --- Response shaping -------------------------------------------------------


def _summary(job: TranslationJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status.value,
        "source_language": job.source_language.value,
        "target_language": job.target_language.value,
        "total_units": job.total_units,
        "progress": job.progress,
    }


def _report_dict(report: RunReport) -> dict[str, Any]:
    return {
        "status": report.job.status.value,
        "succeeded": report.succeeded,
        "total_tokens": report.total_tokens,
        "assignments": len(report.assignments),
        "merges": report.merge_count,
        "merge_tokens": report.merge_tokens,
        "merge_depth": report.merge_depth,
        "verified": report.verified,
        "repairs": report.repairs,
        "assembled_files": [
            {
                "source_path": f.source_path,
                "target_language": f.target_language.value,
                "unit_count": f.unit_count,
                "content": f.content,
            }
            for f in report.assembled_files
        ],
    }
