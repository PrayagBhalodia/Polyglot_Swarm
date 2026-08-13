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

Code isn't prose, though: adjacent chapters share types, signatures, and imports
where they meet, so they can't just be stacked back together. Before a file is
emitted, a second kind of agent — a **reconciler** — merges neighbouring
chapters *pairwise*, folding them up a binary tree until one coherent file
remains. Think of an editor passing over each seam between two translated
chapters, then over the seams between those, and so on.

## Design in one breath

The whole system is built around a few symmetric seams:

- `translate_fn(unit, agent) -> TranslationResult` — translate one chapter.
- `merge_fn(task, agent) -> MergeResult` — reconcile two adjacent chapters.
- `verify_fn(content, language) -> (ok, errors)` — does the merged file parse?
- `repair_fn(request, agent) -> fixed_content` — fix a file the gate rejected.

Everything *except* those functions — chunking, dispatch, lifecycle, the
recursive merge tree, the verification gate, reassembly, persistence, and the
HTTP API — is deterministic coordination logic that builds, runs, and is fully
tested with **zero network access and no API key**. The actual intelligence (the
Groq calls that translate, reconcile, and repair) plugs into those seams; local
stubs stand in everywhere else — except `verify_fn`, whose default is a genuine
`ast.parse`. There are **no runtime dependencies** — the project is stdlib-only
(Python ≥ 3.11).

## Architecture

```
src/
├── models/       Typed data contracts (the "Contract First" layer)
│   ├── enums.py      Language, JobStatus, UnitStatus, AgentStatus
│   ├── source.py     SourceFile, TranslationUnit  (a "chapter")
│   ├── job.py        TranslationJob  (the aggregate root, derives progress)
│   ├── agent.py      SwarmAgent, AgentAssignment
│   ├── result.py     TranslationResult  (the translate_fn boundary)
│   └── merge.py      MergeTask, MergeResult  (the merge_fn boundary)
├── core/         Coordination logic
│   ├── chunker.py       Cut source files into units (the scissors)
│   ├── orchestrator.py  Lifecycle state machine + dispatch (the manager)
│   ├── merger.py        Reconcile adjacent chapters pairwise (the merge tree)
│   ├── verifier.py      Gate merged output on parse-soundness, repair in a loop
│   ├── assembler.py     Reassemble chapters in order (naive-join fallback)
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
                                        ┌──── merge_fn (pairwise, ◀─────┘
                                        │      recursive up a tree)
                                        ▼
                             verify_fn ──ok?──▶ assemble ──▶ target files
                                 ▲   │
                                 └───┘ repair_fn (fix + re-check, bounded)
```

The merge tree is order-preserving and log-depth: `n` chapters take
`ceil(log2(n))` levels, and every pair at a level can be reconciled by a
different agent, so throughput scales with the swarm. An odd chapter at any
level rides up unchanged to pair on the next, so any chapter count works.

Each merged file then passes a **verification gate** before assembly: a
`verify_fn` parses it (a real `ast.parse` for Python; a structural check
otherwise), and if it fails, a `repair_fn` agent is handed the broken file plus
its diagnostics to fix — re-checked after each attempt, up to a bound. A file
that still fails its repair budget aborts the job rather than emitting broken
code. This is the self-correcting loop: the compiler is the oracle, repair is
the feedback.

A job advances through a **validated lifecycle** — illegal transitions raise
rather than silently corrupting state:

```
PENDING → CHUNKING → DISPATCHED → TRANSLATING → MERGING → VERIFYING → ASSEMBLING → COMPLETED
                                                                    ↘ FAILED (from any active state)
```

(With the merge seam disabled, `TRANSLATING` goes straight to `ASSEMBLING` and
chapters are joined naively; with merge on but no verify seam, `VERIFYING` is
skipped. Both are fallbacks the assembler still serves.)

## HTTP API

The pipeline is exposed as a stdlib-only JSON API (`http.server`, no new deps).
Both seams default to offline stubs, so the server runs fully working endpoints
with **no network and no API key**; real Groq clients are injected in their place
via `build_app(..., translate_fn=..., merge_fn=...)` for production. The
`POST /jobs/{id}/run` response reports the merge tree it built (`merges`,
`merge_depth`, `merge_tokens`) alongside the assembled output.

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
validation → `400`, chunking/merge/assembly → `422`).

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
