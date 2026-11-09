"""SQLite storage: job cache (and, from step 4, the application tracker).

DB location: ./data/jobs.db, or set JOB_SEARCH_DB to override (tests use a temp file).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,      -- "<source>:<board>:<native id>"
    source      TEXT NOT NULL,
    board       TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT NOT NULL DEFAULT '',
    remote      INTEGER NOT NULL DEFAULT 0,
    department  TEXT,
    url         TEXT NOT NULL,
    posted_at   TEXT,                  -- ISO-8601 UTC, sortable as text
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_board  ON jobs(source, board);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at);

-- One row per board: when we last fetched it successfully.
CREATE TABLE IF NOT EXISTS board_fetches (
    source     TEXT NOT NULL,
    board      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,          -- ISO-8601 UTC
    job_count  INTEGER NOT NULL,
    PRIMARY KEY (source, board)
);

-- Application tracker. Job fields are SNAPSHOTTED here because the jobs table is a
-- cache: postings vanish from it when they close, but your application history must not.
CREATE TABLE IF NOT EXISTS applications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT UNIQUE,            -- NULL for jobs added manually (e.g. found on LinkedIn)
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,          -- ISO-8601 UTC
    updated_at  TEXT NOT NULL           -- time of the latest event
);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);

-- Every status change or note. Gives a full history and makes follow-ups a simple query.
CREATE TABLE IF NOT EXISTS application_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,       -- status after this event
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id);

-- USCIS H-1B Employer Data Hub rows (one per employer per fiscal year per tax id/location).
CREATE TABLE IF NOT EXISTS h1b_employers (
    fiscal_year          INTEGER NOT NULL,
    employer             TEXT NOT NULL,     -- as printed by USCIS
    employer_norm        TEXT NOT NULL,     -- normalized for matching (see h1b.normalize_name)
    tax_id               TEXT,
    city                 TEXT,
    state                TEXT,
    initial_approvals    INTEGER NOT NULL DEFAULT 0,
    initial_denials      INTEGER NOT NULL DEFAULT 0,
    continuing_approvals INTEGER NOT NULL DEFAULT 0,
    continuing_denials   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_h1b_norm ON h1b_employers(employer_norm);

-- Result of matching each companies.yaml entry to USCIS employer names.
-- Computed once at load time (fuzzy matching is too slow/noisy to do per query).
CREATE TABLE IF NOT EXISTS company_h1b_map (
    source        TEXT NOT NULL,
    board         TEXT NOT NULL,
    company       TEXT NOT NULL,
    employer_norm TEXT NOT NULL,
    method        TEXT NOT NULL,            -- 'override' | 'exact' | 'prefix' | 'fuzzy'
    score         REAL NOT NULL,
    PRIMARY KEY (source, board, employer_norm)
);

-- Per-company totals over the recent window, used by search_jobs (sponsors_only).
CREATE TABLE IF NOT EXISTS company_sponsors (
    source           TEXT NOT NULL,
    board            TEXT NOT NULL,
    company          TEXT NOT NULL,
    recent_approvals INTEGER NOT NULL,
    recent_years     TEXT NOT NULL,         -- e.g. "2022-2026"
    PRIMARY KEY (source, board)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("JOB_SEARCH_DB", DEFAULT_PATH))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # searches can read while a background refresh writes
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
