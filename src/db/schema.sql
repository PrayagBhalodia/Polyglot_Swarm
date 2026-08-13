-- Polyglot Swarm persistence schema (SQLite).
-- Applied idempotently by db.connection.Database.init_schema().

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    source_language   TEXT NOT NULL,
    target_language   TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    language    TEXT NOT NULL,
    content     TEXT NOT NULL,
    sha256      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_file_id     TEXT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    idx                INTEGER NOT NULL,
    content            TEXT NOT NULL,
    source_language    TEXT NOT NULL,
    target_language    TEXT NOT NULL,
    start_line         INTEGER NOT NULL,
    end_line           INTEGER NOT NULL,
    status             TEXT NOT NULL,
    assigned_agent_id  TEXT,
    sha256             TEXT NOT NULL,
    UNIQUE (job_id, idx)
);

CREATE TABLE IF NOT EXISTS results (
    unit_id             TEXT PRIMARY KEY REFERENCES units(id) ON DELETE CASCADE,
    target_language     TEXT NOT NULL,
    translated_content  TEXT NOT NULL,
    agent_id            TEXT,
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    success             INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_job ON units(job_id);
CREATE INDEX IF NOT EXISTS idx_source_files_job ON source_files(job_id);
