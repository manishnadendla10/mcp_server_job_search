import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file — never touches data/jobs.db."""
    monkeypatch.setenv("JOB_SEARCH_DB", str(tmp_path / "test.db"))
    yield tmp_path / "test.db"


@pytest.fixture
def cached_job():
    """Insert one job into the cache, as if search_jobs had fetched it."""
    conn = db.connect()
    with conn:
        conn.execute(
            """INSERT INTO jobs (id, source, board, company, title, location, remote, url, posted_at, description)
               VALUES ('greenhouse:acme:1', 'greenhouse', 'acme', 'Acme', 'Backend Engineer',
                       'Dallas, TX', 0, 'https://example.com/1', '2026-11-01T00:00:00+00:00', 'Build APIs.')"""
        )
    conn.close()
    return "greenhouse:acme:1"
