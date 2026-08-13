# Polyglot Swarm

> An automated software-translation factory. Feed it a legacy codebase, press a
> button, and a swarm of AI agents translates it into a modern language **in
> parallel** — then an Orchestrator tapes the translated "chapters" back together
> into a working application, all reachable over a small HTTP API.

## The idea, in one analogy

Translating a 1,000-page manual from ancient Greek to English:

- **The old way** — one translator, page 1 to 1,000, a year of work.
- **The Polyglot Swarm way** — cut the book into 20 chapters, hand them to 20
  translators working at once, and a manager reassembles the chapters in order.

Here the "translators" are Groq-powered agents; the "manager" is the
**Orchestrator**; the "scissors" is the **Chunker**.

## Design in one breath

The whole system is built around a single seam:
`translate_fn(unit, agent) -> TranslationResult`. Everything *except* that one
function — chunking, dispatch, lifecycle, reassembly, persistence, and the HTTP
API — is deterministic coordination logic that builds, runs, and is fully tested
with **zero network access and no API key**. The actual intelligence (the Groq
call that turns COBOL into Python) plugs into that seam; a local stub stands in
everywhere else. There are **no runtime dependencies** — the project is
stdlib-only (Python ≥ 3.11).

## Architecture

```
src/
├── models/       Typed data contracts (the "Contract First" layer)
│   ├── enums.py      Language, JobStatus, UnitStatus, AgentStatus
│   ├── source.py     SourceFile, TranslationUnit  (a "chapter")
│   ├── job.py        TranslationJob  (the aggregate root, derives progress)
│   ├── agent.py      SwarmAgent, AgentAssignment
│   └── result.py     TranslationResult  (the translate_fn boundary)
├── core/         Coordination logic
│   ├── chunker.py       Cut source files into units (the scissors)
│   ├── orchestrator.py  Lifecycle state machine + dispatch (the manager)
│   ├── assembler.py     Reassemble translated chapters in order
│   └── errors.py        Exception hierarchy
├── config/       Layered settings (defaults → TOML → env); secrets only in env
├── db/           SQLite data-access layer (repositories, never touched above db/)
│   └── repository.py    JobRepository (also the orchestrator's checkpoint hook)
├── services/     Use cases over the core (TranslationService); the translate_fn seam
├── controllers/  HTTP handlers: parse/validate a request → service call → response
├── routes/       The Router (templated path matching) + the route table
├── middleware/   JSON body parsing, error→JSON mapping, request logging
└── api/          Transport (Request/Response), Application dispatch, stdlib server
```

The layering is strict and one-directional: the HTTP layer
(`api` → `routes`/`controllers` → `services`) depends on the core, never the
reverse. `core` stays independent of `db` (it checkpoints through a small
`JobPersister` protocol), so the coordination logic can be tested in complete
isolation.

## Data flow

```
legacy files ──chunk──▶ units ──dispatch──▶ agents ──translate_fn──▶ results
                                                                        │
   assembled target files ◀──assemble── (ordered by unit index) ◀──────┘
```

A job advances through a **validated lifecycle** — illegal transitions raise
rather than silently corrupting state:

```
PENDING → CHUNKING → DISPATCHED → TRANSLATING → ASSEMBLING → COMPLETED
                                                            ↘ FAILED (from any active state)
```

## HTTP API

The pipeline is exposed as a stdlib-only JSON API (`http.server`, no new deps).
The translation seam defaults to an offline stub, so the server runs fully
working endpoints with **no network and no API key**; a real Groq client is
injected in its place via `build_app(..., translate_fn=...)` for production.

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness probe |
| `POST /jobs` | Create a job (`name`, `source_language`, `target_language`, `source_files[]`) |
| `GET /jobs` | List job summaries |
| `GET /jobs/{id}` | Fetch one job (full aggregate) |
| `POST /jobs/{id}/run` | Run the translate pipeline; returns the job + assembled output |
| `GET /jobs/{id}/units` | The job's translation units |
| `GET /jobs/{id}/results` | Stored per-unit results |
| `GET /agents` | The configured swarm |

Errors return a consistent body — `{"error": {"status": 404, "message": "..."}}` —
with domain failures mapped to HTTP status (illegal lifecycle → `409`,
validation → `400`, chunking/assembly → `422`).

## Quick start

```bash
# 1. Run the full local verification gate (compile + typecheck + tests).
./scripts/verify.sh

# 2. See the whole pipeline run end-to-end with a stubbed Brain (no API key):
python scripts/demo_pipeline.py

# 3. Create the persistent SQLite database:
python scripts/init_db.py

# 4. Serve the HTTP API (127.0.0.1:8000; POLYGLOT_API_PORT overrides):
python scripts/serve_api.py
```

Install `mypy` (`pip install -e '.[dev]'`) to enable the strict type check inside
`verify.sh`; without it, that step is skipped and the rest still runs.

## Configuration

Precedence, lowest to highest: `src/config/default.toml` → an optional user TOML
→ environment variables. **Secrets live only in the environment.**

| Setting | Env var | Default |
|---|---|---|
| Agent count | `POLYGLOT_AGENT_COUNT` | `8` |
| Target language | `POLYGLOT_TARGET_LANGUAGE` | `python` |
| Max lines / unit | `POLYGLOT_MAX_LINES_PER_UNIT` | `200` |
| DB path | `POLYGLOT_DB_PATH` | `polyglot_swarm.db` |
| API host / port | `POLYGLOT_API_HOST` / `POLYGLOT_API_PORT` | `127.0.0.1` / `8000` |
| Groq model | `POLYGLOT_GROQ_MODEL` | `llama-3.3-70b-versatile` |
| Groq API key | `GROQ_API_KEY` | *(required only when a real Brain is plugged in)* |

## Commit discipline

Every commit passes `./scripts/verify.sh` cleanly (`exit 0`) before it is made:
byte-compilation, an optional strict `mypy` check, and the full unittest suite.
No broken, non-compiling, or failing code is ever committed.
