"""Application tracker: saved -> applied -> interviewing -> offer / rejected / withdrawn.

Every change is an event in application_events, so the tracker keeps full history
and "follow-ups due" is just "latest event older than N days".
Functions raise ValueError for bad input; server.py turns that into a tool error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db

STATUSES = ("saved", "applied", "interviewing", "offer", "rejected", "withdrawn")
ACTIVE_STATUSES = ("applied", "interviewing")  # the ones that need follow-ups


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _check_status(status: str) -> str:
    s = status.strip().lower()
    if s not in STATUSES:
        raise ValueError(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}.")
    return s


def _row_to_dict(row, now: datetime) -> dict:
    d = dict(row)
    d["days_since_update"] = (now - datetime.fromisoformat(d["updated_at"])).days
    if "posting_open" in d:
        d["posting_open"] = bool(d["posting_open"])
    return d


def _add_event(conn, app_id: int, status: str, note: str | None, at: datetime) -> None:
    conn.execute(
        "INSERT INTO application_events (application_id, status, note, created_at) VALUES (?, ?, ?, ?)",
        (app_id, status, note, _iso(at)),
    )
    conn.execute("UPDATE applications SET status = ?, updated_at = ? WHERE id = ?", (status, _iso(at), app_id))


# ---------------------------------------------------------------- commands

def save_job(
    job_id: str | None = None,
    notes: str | None = None,
    status: str = "saved",
    company: str | None = None,
    title: str | None = None,
    url: str | None = None,
    location: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Track a job. Either job_id (from search) or company+title (manual entry)."""
    now = now or _now()
    status = _check_status(status)
    conn = db.connect()
    try:
        with conn:
            if job_id:
                existing = conn.execute("SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
                if existing:
                    if notes:
                        cur = conn.execute("SELECT status FROM applications WHERE id = ?", (existing["id"],)).fetchone()
                        _add_event(conn, existing["id"], cur["status"], notes, now)
                    app = get_application(existing["id"], conn=conn, now=now)
                    app["already_tracked"] = True
                    return app
                job = conn.execute("SELECT company, title, location, url FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if job is None and not (company and title):
                    raise ValueError(
                        f"Job '{job_id}' isn't in the cache (it may have closed). "
                        "Pass company and title to track it manually."
                    )
                if job is not None:
                    company, title = job["company"], job["title"]
                    location, url = location or job["location"], url or job["url"]
            elif not (company and title):
                raise ValueError("Pass a job_id from search_jobs, or company and title for a manual entry.")

            cur = conn.execute(
                """INSERT INTO applications (job_id, company, title, location, url, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, company, title, location, url, status, _iso(now), _iso(now)),
            )
            _add_event(conn, cur.lastrowid, status, notes, now)
            return get_application(cur.lastrowid, conn=conn, now=now)
    finally:
        conn.close()


def update_application(
    application_id: int,
    status: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Change status and/or add a note. A note alone still counts as activity (resets follow-up clock)."""
    if status is None and not note:
        raise ValueError("Pass a new status, a note, or both.")
    now = now or _now()
    conn = db.connect()
    try:
        with conn:
            row = conn.execute("SELECT status FROM applications WHERE id = ?", (application_id,)).fetchone()
            if row is None:
                raise ValueError(f"No application with id {application_id}. Use list_applications to find ids.")
            new_status = _check_status(status) if status else row["status"]
            _add_event(conn, application_id, new_status, note, now)
            return get_application(application_id, conn=conn, now=now)
    finally:
        conn.close()


# ---------------------------------------------------------------- queries

_SELECT = """
SELECT a.*,
       EXISTS(SELECT 1 FROM jobs j WHERE j.id = a.job_id) AS posting_open
FROM applications a
"""


def get_application(application_id: int, conn=None, now: datetime | None = None) -> dict:
    now = now or _now()
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(_SELECT + " WHERE a.id = ?", (application_id,)).fetchone()
        if row is None:
            raise ValueError(f"No application with id {application_id}.")
        app = _row_to_dict(row, now)
        app["history"] = [
            dict(e) for e in conn.execute(
                "SELECT status, note, created_at FROM application_events WHERE application_id = ? ORDER BY id",
                (application_id,),
            )
        ]
        return app
    finally:
        if own:
            conn.close()


def list_applications(
    statuses: list[str] | None = None,
    company: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    where, params = [], []
    if statuses:
        checked = [_check_status(s) for s in statuses]
        where.append(f"a.status IN ({','.join('?' * len(checked))})")
        params += checked
    if company:
        where.append("a.company LIKE ?")
        params.append(f"%{company}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    conn = db.connect()
    try:
        rows = conn.execute(f"{_SELECT} {clause} ORDER BY a.updated_at DESC LIMIT ?", params + [limit]).fetchall()
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())
    finally:
        conn.close()
    return {
        "counts_by_status": {s: counts.get(s, 0) for s in STATUSES},
        "applications": [_row_to_dict(r, now) for r in rows],
    }


def followups_due(days: int = 7, statuses: list[str] | None = None, now: datetime | None = None) -> dict:
    """Active applications with no activity (status change or note) in `days` days. Oldest first."""
    now = now or _now()
    checked = [_check_status(s) for s in (statuses or ACTIVE_STATUSES)]
    cutoff = _iso(now - timedelta(days=days))
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""{_SELECT} WHERE a.status IN ({','.join('?' * len(checked))}) AND a.updated_at <= ?
                ORDER BY a.updated_at ASC""",
            checked + [cutoff],
        ).fetchall()
    finally:
        conn.close()
    return {
        "threshold_days": days,
        "statuses_checked": checked,
        "due": [_row_to_dict(r, now) for r in rows],
    }
