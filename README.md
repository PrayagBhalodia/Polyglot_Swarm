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
**Orchestrator**; the "scissors" is the **Chunker** — and it cuts *between*
chapters, not through them: each unit boundary is pulled back to the nearest
top-level construct (`def`/`class`/`func`/`fn`, a COBOL `DIVISION`, a
blank-line paragraph break) that still fits the line budget, because half a
function is the worst thing you can hand a translator.

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
tested with **zero network access and no API key**. The actual intelligence
plugs into those seams: a real, stdlib-only **Groq client** (OpenAI-compatible
chat completions over `urllib` — no SDK) backs `translate_fn`, `merge_fn`, and
`repair_fn` when `GROQ_API_KEY` is set; local stubs stand in otherwise, and
`verify_fn` is always a genuine `ast.parse`. There are **no runtime
dependencies** — the project is stdlib-only (Python ≥ 3.11).

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
│   └── repository.py    JobRepository (also the orchestrator's checkpoint hook),
│                        ResultRepository, OutputRepository (durable run output)
├── services/     Use cases over the core (TranslationService); the translate_fn seam
│   └── job_runner.py    Background runs, each on its own DB connection
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

**The swarm is genuinely concurrent.** Units are translated in a thread pool
bounded by `POLYGLOT_MAX_CONCURRENCY` (default: one worker per agent); each
level of the merge tree dispatches all of its independent pairs at once and
waits for them before descending; files are verified in parallel too. Only the
seams ever run on a worker thread — agent/unit state changes and every
persistence checkpoint happen on the main thread as futures land, so the SQLite
connection is never shared across threads. Results are keyed by unit id and
assembled by `index`, so **completion order can never change the output**: a
parallel run is byte-identical to a sequential one, tokens included.

Each merged file then passes a **verification gate** before assembly: a
`verify_fn` parses it, and if it fails, a `repair_fn` agent is handed the broken
file plus its diagnostics to fix — re-checked after each attempt, up to
`POLYGLOT_MAX_REPAIR_ATTEMPTS`. A file that still fails its repair budget aborts
the job rather than emitting broken code. This is the self-correcting loop: the
compiler is the oracle, repair is the feedback.

For that loop to mean anything the oracle has to be real, so the default gate
(`POLYGLOT_VERIFY=toolchain`) uses an actual syntax checker per target language
whenever one is installed, and turns its diagnostics into the error list the
repair agent sees:

| Target | Checker | | Target | Checker |
|---|---|---|---|---|
| python | `ast.parse` (in-process) | | rust | `rustc --emit=metadata` |
| javascript | `node --check` | | php | `php -l` |
| typescript | `tsc --noEmit` | | ruby | `ruby -c` |
| go | `gofmt -e` | | *anything else* | structural check |

Nothing here executes the translated program — every command is a parse or type
check — and **a missing tool is never a failure**: `shutil.which` decides, and
an absent (or hung, or unlaunchable) tool degrades to the structural check.
`POLYGLOT_VERIFY=basic` keeps the gate pure-stdlib and never starts a
subprocess, which is what the test suite uses.

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
via `build_app(..., translate_fn=..., merge_fn=...)` for production.

| Method & path | Purpose |
|---|---|
| `GET /` , `GET /ui` | The web UI (single self-contained HTML page) |
| `GET /health` | Liveness probe |
| `POST /jobs` | Create a job (`name`, `source_language`, `target_language`, `source_files[]`) |
| `POST /jobs/ingest` | Create a job from a `source_kind` (`local`/`github`) + `location` |
| `GET /jobs` | List job summaries |
| `GET /jobs/{id}` | Fetch one job (full aggregate, including `progress`) |
| `POST /jobs/{id}/run` | **Start** the pipeline → `202 Accepted`; `?wait=1` runs it inline and returns the full report |
| `GET /jobs/{id}/units` | The job's translation units |
| `GET /jobs/{id}/results` | Stored per-unit results |
| `GET /jobs/{id}/output` | Assembled files + merge/verify statistics of the last run |
| `GET /agents` | The configured swarm |

### Runs are asynchronous

Translating a real repository takes minutes, so `POST /jobs/{id}/run` no longer
holds the socket open for the pipeline. It hands the job to a **JobRunner**
thread and returns `202 Accepted` immediately with a `poll` and an `output`
link:

```
POST /jobs/{id}/run            →  202  {"job_id": ..., "poll": "/jobs/{id}", ...}
GET  /jobs/{id}       (poll)   →  200  {"status": "translating", "progress": 0.62, ...}
GET  /jobs/{id}/output         →  200  {"succeeded": true, "assembled_files": [...], ...}
```

`progress` climbs while the run is in flight because the pipeline checkpoints
job and unit state to SQLite as units land. Output is **persisted** (a
`run_reports` row plus one `assembled_files` row per file), so it survives the
process that produced it — and a failed run stores its error there too.

Append `?wait=1` to run the pipeline synchronously and get the whole report back
in the response; that is the deterministic path the end-to-end tests use.

> The HTTP server is deliberately **single-threaded** (`http.server.HTTPServer`),
> and a `sqlite3` connection may not cross threads — so every background run
> opens its **own** connection via `Database.sibling()` and closes it when the
> thread ends. Nothing is shared between the request thread and a runner.

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

# 4. Serve the HTTP API + web UI (127.0.0.1:8000; POLYGLOT_API_PORT overrides):
python scripts/serve_api.py
```

Then open <http://127.0.0.1:8000> for the web UI: paste a **GitHub URL** or a
**local folder path**, pick a **destination language**, and hit *Translate*. The
server ingests the sources (a shallow `git clone` for GitHub, a directory walk
for a local path), runs the full pipeline, and renders the assembled output with
its merge/verify stats. Runs offline with the stub Brain — no API key needed.

Install `mypy` (`pip install -e '.[dev]'`) to enable the strict type check inside
`verify.sh`; without it, that step is skipped and the rest still runs.

## Configuration

Precedence, lowest to highest: `src/config/default.toml` → an optional user TOML
→ environment variables. **Secrets live only in the environment.**

| Setting | Env var | Default |
|---|---|---|
| Agent count | `POLYGLOT_AGENT_COUNT` | `8` |
| Max concurrent seam calls | `POLYGLOT_MAX_CONCURRENCY` | *(= agent count)* |
| Repair budget per file | `POLYGLOT_MAX_REPAIR_ATTEMPTS` | `1` |
| Target language | `POLYGLOT_TARGET_LANGUAGE` | `python` |
| Max lines / unit | `POLYGLOT_MAX_LINES_PER_UNIT` | `200` |
| Chunk strategy | `POLYGLOT_CHUNK_STRATEGY` | `structural` (or `lines`) |
| Verification gate | `POLYGLOT_VERIFY` | `toolchain` (or `basic`) |
| DB path | `POLYGLOT_DB_PATH` | `polyglot_swarm.db` |
| API host / port | `POLYGLOT_API_HOST` / `POLYGLOT_API_PORT` | `127.0.0.1` / `8000` |
| Groq model | `POLYGLOT_GROQ_MODEL` | `llama-3.3-70b-versatile` |
| Groq API key | `GROQ_API_KEY` | *(unset ⇒ offline stubs; set ⇒ real Groq)* |

**One key drives the whole swarm.** Agents are concurrent requests, not separate
credentials, so a single `GROQ_API_KEY` powers any number of agents; Groq's rate
limits are per *account*, and the client retries `429`/transient errors with
backoff. Export the key and the translate/merge/repair seams switch from the
offline stubs to real Groq automatically — no code change:

```bash
export GROQ_API_KEY=gsk_...
python scripts/serve_api.py     # logs "Brain: Groq (model=...)"
```

Prefer a file? Copy `.env.example` to `.env`, paste your key, and run the server
— `serve_api.py` loads `.env` on startup via a tiny stdlib loader (no new deps).
`.env` is gitignored; an exported shell variable still takes precedence over it.

## Commit discipline

Every commit passes `./scripts/verify.sh` cleanly (`exit 0`) before it is made:
byte-compilation, an optional strict `mypy` check, and the full unittest suite.
No broken, non-compiling, or failing code is ever committed.
