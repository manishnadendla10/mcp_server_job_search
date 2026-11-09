"""Job catalog: fetch boards into the SQLite cache, then search locally.

Flow:  companies.yaml -> adapters (concurrent HTTP) -> jobs table -> SQL search.
Boards are re-fetched only when their cache is older than CACHE_TTL, so most
searches never touch the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

import db
from sources import SOURCES, Job

log = logging.getLogger("job-search-mcp.catalog")

CONFIG_PATH = Path(__file__).parent / "companies.yaml"
CACHE_TTL = timedelta(hours=6)
MAX_CONCURRENCY = 10
MAX_DESCRIPTION_CHARS = 15_000


def load_companies(path: Path = CONFIG_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["companies"]


def _utc_iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


# ---------------------------------------------------------------- refresh

def _stale_boards(conn, companies: list[dict], force: bool) -> list[dict]:
    if force:
        return companies
    cutoff = _utc_iso(datetime.now(timezone.utc) - CACHE_TTL)
    fresh = {
        (r["source"], r["board"])
        for r in conn.execute("SELECT source, board FROM board_fetches WHERE fetched_at >= ?", (cutoff,))
    }
    return [c for c in companies if (c["source"], c["board"]) not in fresh]


def stale_count(force: bool = False) -> int:
    """How many boards a refresh would fetch right now."""
    conn = db.connect()
    try:
        return len(_stale_boards(conn, load_companies(), force))
    finally:
        conn.close()


def _store_board(conn, source: str, board: str, jobs: list[Job]) -> None:
    """Replace a board's jobs atomically, so closed postings disappear."""
    with conn:
        conn.execute("DELETE FROM jobs WHERE source = ? AND board = ?", (source, board))
        conn.executemany(
            """INSERT OR REPLACE INTO jobs
               (id, source, board, company, title, location, remote, department, url, posted_at, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (j.id, j.source, j.board, j.company, j.title, j.location, int(j.remote),
                 j.department, j.url, _utc_iso(j.posted_at), j.description)
                for j in jobs
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO board_fetches (source, board, fetched_at, job_count) VALUES (?, ?, ?, ?)",
            (source, board, _utc_iso(datetime.now(timezone.utc)), len(jobs)),
        )


RETRIES = 3


def _is_transient(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):  # connect errors, timeouts, resets
        return True
    return isinstance(e, httpx.HTTPStatusError) and (
        e.response.status_code == 429 or e.response.status_code >= 500
    )


async def _fetch_with_retry(client: httpx.AsyncClient, c: dict) -> list[Job]:
    """Retry transient failures with exponential backoff (1s, 2s). 404s fail fast."""
    for attempt in range(RETRIES):
        try:
            return await SOURCES[c["source"]].fetch(client, c["board"], c["name"], options=c)
        except Exception as e:
            if attempt == RETRIES - 1 or not _is_transient(e):
                raise
            log.info("retrying %s after %s (attempt %d)", c["name"], type(e).__name__, attempt + 1)
            await asyncio.sleep(2 ** attempt)
    raise AssertionError("unreachable")


async def refresh(force: bool = False, companies: list[dict] | None = None) -> dict:
    """Fetch stale (or all, if force) boards. A failing board keeps its old cache."""
    companies = companies if companies is not None else load_companies()
    conn = db.connect()
    todo = _stale_boards(conn, companies, force)
    if not todo:
        return {"refreshed": 0, "failed": [], "seconds": 0.0}

    started = time.monotonic()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(client, c):
        async with sem:
            try:
                return c, await _fetch_with_retry(client, c), None
            except Exception as e:  # noqa: BLE001 — one bad board must not break the rest
                return c, None, f"{type(e).__name__}: {e}"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        results = await asyncio.gather(*(one(client, c) for c in todo))

    failed = []
    for c, jobs, err in results:
        if err:
            log.warning("fetch failed for %s (%s:%s): %s", c["name"], c["source"], c["board"], err)
            failed.append({"company": c["name"], "error": err})
        else:
            _store_board(conn, c["source"], c["board"], jobs)
    conn.close()
    return {
        "refreshed": len(todo) - len(failed),
        "failed": failed,
        "seconds": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------- search

def _any_like(columns: list[str], terms: list[str], params: list) -> str:
    """SQL fragment: any term appears (case-insensitive) in any of the columns."""
    parts = []
    for term in terms:
        for col in columns:
            parts.append(f"{col} LIKE ?")
            params.append(f"%{term.strip()}%")
    return "(" + " OR ".join(parts) + ")"


def search(
    keywords: list[str] | None = None,
    locations: list[str] | None = None,
    remote: bool | None = None,
    posted_within_days: int | None = None,
    companies: list[str] | None = None,
    search_descriptions: bool = False,
    limit: int = 20,
    sponsors_only: bool = False,
    min_h1b_approvals: int = 1,
) -> dict:
    where, params = [], []

    if sponsors_only:
        where.append("COALESCE(s.recent_approvals, 0) >= ?")
        params.append(min_h1b_approvals)

    keywords = [k for k in (keywords or []) if k.strip()]
    if keywords:
        cols = ["jobs.title", "jobs.department"] + (["jobs.description"] if search_descriptions else [])
        where.append(_any_like(cols, keywords, params))

    locations = [loc for loc in (locations or []) if loc.strip()]
    if locations:
        where.append(_any_like(["jobs.location"], locations, params))

    if remote is not None:
        where.append("jobs.remote = ?")
        params.append(int(remote))

    if posted_within_days is not None:
        where.append("jobs.posted_at >= ?")
        params.append(_utc_iso(datetime.now(timezone.utc) - timedelta(days=posted_within_days)))

    companies = [c for c in (companies or []) if c.strip()]
    if companies:
        where.append(_any_like(["jobs.company", "jobs.board"], companies, params))

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # LEFT JOIN: H-1B totals per board (empty until `python h1b.py load` has run).
    base = "FROM jobs LEFT JOIN company_sponsors s ON s.source = jobs.source AND s.board = jobs.board"
    conn = db.connect()
    total = conn.execute(f"SELECT COUNT(*) {base} {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT jobs.id, jobs.title, jobs.company, jobs.location, jobs.remote, jobs.posted_at, jobs.url,
                   s.recent_approvals
            {base} {clause}
            ORDER BY jobs.posted_at DESC NULLS LAST LIMIT ?""",
        params + [max(1, min(limit, 100))],
    ).fetchall()
    conn.close()

    jobs = []
    for r in rows:
        job = {
            "id": r["id"],
            "title": r["title"],
            "company": r["company"],
            "location": r["location"],
            "remote": bool(r["remote"]),
            "posted": r["posted_at"][:10] if r["posted_at"] else None,
            "url": r["url"],
        }
        if r["recent_approvals"] is not None:
            job["h1b_approvals_5y"] = r["recent_approvals"]
        jobs.append(job)
    return {"total_matches": total, "returned": len(jobs), "jobs": jobs}


async def get_job(job_id: str) -> dict | None:
    conn = db.connect()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    job = dict(row)
    job["remote"] = bool(job["remote"])

    # Some sources (Workday) don't include descriptions in listings: fetch once, cache.
    if not job.get("description"):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                desc = await SOURCES[job["source"]].fetch_description(client, job)
        except Exception as e:  # noqa: BLE001
            log.warning("description fetch failed for %s: %s", job_id, e)
            desc = None
        if desc:
            job["description"] = desc
            with conn:
                conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (desc, job_id))
    conn.close()

    desc = job.get("description") or ""
    if len(desc) > MAX_DESCRIPTION_CHARS:
        job["description"] = desc[:MAX_DESCRIPTION_CHARS] + "\n\n[... truncated]"
    return job


def cache_status() -> dict:
    conn = db.connect()
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    boards = conn.execute("SELECT COUNT(*), MIN(fetched_at) FROM board_fetches").fetchone()
    conn.close()
    return {"jobs_cached": jobs, "boards_cached": boards[0], "oldest_fetch": boards[1]}
