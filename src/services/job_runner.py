"""Run translation jobs on background threads so the HTTP request can return.

A real repository takes minutes to translate: hundreds of units, each one a
network round trip. Doing that inside ``POST /jobs/{id}/run`` means the socket
is held open for the whole pipeline — the client times out, the UI hangs, and
nothing can report progress because the only thread that knows anything is busy.

:class:`JobRunner` moves the pipeline onto its own thread and hands the request
straight back (``202 Accepted``). Progress is not pushed anywhere: the run
checkpoints job and unit state to SQLite as it advances, so any poller reading
``GET /jobs/{id}`` sees ``status`` and ``progress`` climb, and the final output
is durable in the database rather than trapped in a response body.

**Connection discipline.** ``sqlite3`` connections are single-threaded, and the
HTTP server itself is single-threaded, so a worker must never borrow the request
thread's connection. Each run therefore opens its **own** :class:`Database` via
:meth:`Database.sibling` and closes it when the thread ends; the service (and
its repositories) used inside the thread are built on that connection alone.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from config.settings import Settings
from core.errors import PolyglotSwarmError
from core.merger import MergeFn
from core.orchestrator import ExtractContractFn, TranslateFn
from core.verifier import RepairFn, VerifyFn
from db.connection import Database
from services.translation_service import TranslationService

_logger = logging.getLogger("polyglot.runner")

# Builds the (connection, service) pair a worker thread will use. The worker
# owns both and closes the connection when it is done.
ServiceFactory = Callable[[], tuple[Database, TranslationService]]


class JobRunner:
    """Starts jobs on daemon threads and remembers how each one ended."""

    def __init__(self, service_factory: ServiceFactory) -> None:
        self._factory = service_factory
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._errors: dict[str, str] = {}

    def start(self, job_id: str) -> bool:
        """Begin running ``job_id`` in the background.

        Returns ``False`` (and starts nothing) if a run for this job is already
        in flight, so a double-click cannot translate the same repo twice.
        """
        with self._lock:
            running = self._threads.get(job_id)
            if running is not None and running.is_alive():
                return False
            self._errors.pop(job_id, None)
            thread = threading.Thread(
                target=self._run,
                args=(job_id,),
                name=f"polyglot-job-{job_id[:8]}",
                daemon=True,
            )
            self._threads[job_id] = thread
        thread.start()
        return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(job_id)
        return thread is not None and thread.is_alive()

    def error_for(self, job_id: str) -> str | None:
        """The message from the last failed run of ``job_id``, if any."""
        with self._lock:
            return self._errors.get(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until ``job_id``'s run finishes; ``True`` if it did.

        Only a convenience for tests and shutdown — the HTTP path never waits.
        """
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # --- The worker ---------------------------------------------------------

    def _run(self, job_id: str) -> None:
        db, service = self._factory()
        try:
            job = service.get_job(job_id)
            if job is None:  # deleted between the request and the thread start
                self._record(job_id, f"job {job_id!r} disappeared before it ran")
                return
            service.run(job)
        except PolyglotSwarmError as exc:
            # The orchestrator has already moved the job to FAILED and
            # checkpointed it; keep the reason so a poller can read it back.
            self._record(job_id, str(exc))
            self._persist_failure(service, job_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            _logger.exception("job %s crashed", job_id)
            self._record(job_id, f"internal error: {exc}")
            self._persist_failure(service, job_id, f"internal error: {exc}")
        finally:
            db.close()

    def _record(self, job_id: str, message: str) -> None:
        with self._lock:
            self._errors[job_id] = message

    @staticmethod
    def _persist_failure(
        service: TranslationService, job_id: str, message: str
    ) -> None:
        try:
            job = service.get_job(job_id)
            if job is not None:
                service.record_failure(job, message)
        except Exception:  # noqa: BLE001 - best effort; the thread is ending
            _logger.exception("could not persist failure for job %s", job_id)


def build_job_runner(
    db: Database,
    *,
    settings: Settings,
    translate_fn: TranslateFn | None = None,
    merge_fn: MergeFn | None = None,
    verify_fn: VerifyFn | None = None,
    repair_fn: RepairFn | None = None,
    extract_contract_fn: ExtractContractFn | None = None,
) -> JobRunner:
    """A :class:`JobRunner` whose workers mirror ``db``'s wiring on their own
    connection."""

    def factory() -> tuple[Database, TranslationService]:
        worker_db = db.sibling()
        return worker_db, TranslationService(
            worker_db,
            settings=settings,
            translate_fn=translate_fn,
            merge_fn=merge_fn,
            verify_fn=verify_fn,
            repair_fn=repair_fn,
            extract_contract_fn=extract_contract_fn,
        )

    return JobRunner(factory)
