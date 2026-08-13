# Polyglot Swarm — Track A: Core & Infrastructure

> An automated software-translation factory. Feed it a legacy codebase, press a
> button, and a swarm of AI agents translates it into a modern language **in
> parallel** — then an Orchestrator tapes the translated "chapters" back together
> into a working application.

This repository is **Track A** of the Polyglot Swarm: the core application logic,
data contracts, database access layer, configuration, and local verification.

## The idea, in one analogy

Translating a 1,000-page manual from ancient Greek to English:

- **The old way** — one translator, page 1 to 1,000, a year of work.
- **The Polyglot Swarm way** — cut the book into 20 chapters, hand them to 20
  translators working at once, and a manager reassembles the chapters in order.

Here the "translators" are Groq-powered agents; the "manager" is the
**Orchestrator**; the "scissors" is the **Chunker**.

## What Track A owns (and what it deliberately does not)

Track A is the **skeleton and the contracts**. It does *not* make the Groq API
call itself — that is the **Brain** track. Instead Track A exposes a single seam,
`translate_fn(unit, agent) -> TranslationResult`, that the Brain plugs into. That
separation is what lets this whole codebase build, run, and be fully tested
**with zero network access and no API key**.

```
src/
├── models/      Typed data contracts (the "Contract First" layer)
│   ├── enums.py     Language, JobStatus, UnitStatus, AgentStatus
│   ├── source.py    SourceFile, TranslationUnit  (a "chapter")
│   ├── job.py       TranslationJob  (the aggregate root, derives progress)
│   ├── agent.py     SwarmAgent, AgentAssignment
│   └── result.py    TranslationResult  (Brain → Track A boundary)
├── core/        Coordination logic
│   ├── chunker.py       Cut source files into units (the scissors)
│   ├── orchestrator.py  Lifecycle state machine + dispatch (the manager)
│   ├── assembler.py     Reassemble translated chapters in order
│   └── errors.py        Exception hierarchy
├── config/      Layered settings (defaults → TOML → env); secrets only in env
│   ├── settings.py
│   └── default.toml
└── db/          SQLite data-access layer (repositories, never touched above db/)
    ├── schema.sql
    ├── connection.py
    └── repository.py    JobRepository (also the orchestrator's checkpoint hook)
```

## Data flow

```
legacy files ──chunk──▶ units ──dispatch──▶ agents ──translate_fn──▶ results
                                                                        │
   assembled target files ◀──assemble── (ordered by unit index) ◀──────┘
```

The job advances through a **validated lifecycle** — illegal transitions raise
rather than silently corrupting state:

```
PENDING → CHUNKING → DISPATCHED → TRANSLATING → ASSEMBLING → COMPLETED
                                                            ↘ FAILED (from any active state)
```

## Quick start

```bash
# 1. Run the full local verification gate (compile + typecheck + tests).
./scripts/verify.sh

# 2. See the whole pipeline run end-to-end with a stubbed Brain (no API key):
python scripts/demo_pipeline.py

# 3. Create the persistent SQLite database:
python scripts/init_db.py
```

There are **no runtime dependencies** — Track A is stdlib-only (Python ≥ 3.11).
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
| Groq model | `POLYGLOT_GROQ_MODEL` | `llama-3.3-70b-versatile` |
| Groq API key | `GROQ_API_KEY` | *(required by the Brain track only)* |

## Commit discipline

Every commit in this track passes `./scripts/verify.sh` cleanly (`exit 0`) before
it is made. No broken, non-compiling, or failing code is ever committed.
