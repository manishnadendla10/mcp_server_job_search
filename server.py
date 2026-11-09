"""Job Search MCP server.

IMPORTANT: this server talks MCP over stdio, so stdout is reserved for the
protocol. Never print() — use the logger, which writes to stderr.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.mcpserver import MCPServer

import catalog
import h1b
import tracker

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
log = logging.getLogger("job-search-mcp")

mcp = MCPServer(
    name="job-search",
    instructions=(
        "Search open roles across company job boards (Greenhouse, Lever, Ashby, Workday), "
        "track job applications, and check H-1B sponsorship history. "
        "Use search_jobs first, then get_job_details for a specific job's full description."
    ),
)


@mcp.tool()
async def search_jobs(
    keywords: Annotated[list[str] | None, Field(
        description="Match ANY of these in the job title or department, e.g. ['backend', 'cloud', 'platform']. "
                    "Use short terms; 'engineer' matches 'Software Engineer'.")] = None,
    locations: Annotated[list[str] | None, Field(
        description="Match ANY of these in the location text, e.g. ['Dallas', 'Austin', 'Texas']. "
                    "Include 'Remote' to also catch remote postings.")] = None,
    remote: Annotated[bool | None, Field(
        description="True = only remote jobs, False = exclude remote jobs, omit = no filter.")] = None,
    posted_within_days: Annotated[int | None, Field(
        description="Only jobs first posted within this many days, e.g. 7 for 'this week'.", ge=1)] = None,
    companies: Annotated[list[str] | None, Field(
        description="Restrict to these companies (name or board slug), e.g. ['Stripe', 'Toast'].")] = None,
    search_descriptions: Annotated[bool, Field(
        description="Also match keywords inside full job descriptions (broader, noisier).")] = False,
    limit: Annotated[int, Field(description="Max jobs to return (1-100).", ge=1, le=100)] = 20,
    sponsors_only: Annotated[bool, Field(
        description="Only companies with H-1B approvals in the last 5 fiscal years (USCIS data).")] = False,
    min_h1b_approvals: Annotated[int, Field(
        description="With sponsors_only: minimum approvals over 5 years, e.g. 50 for frequent sponsors.", ge=1)] = 1,
) -> dict:
    """Search open jobs across all tracked companies. Newest first.

    Returns total_matches plus a compact list (id, title, company, location, remote,
    posted date, url). Use get_job_details(id) for the full description.
    Job data is cached; boards older than 6 hours are refreshed in the background.
    """
    refresh = await _ensure_fresh()
    result = catalog.search(keywords, locations, remote, posted_within_days, companies,
                            search_descriptions, limit, sponsors_only, min_h1b_approvals)
    if sponsors_only and not h1b.has_data():
        result["warnings"] = ["No H-1B data is loaded, so sponsors_only matched nothing. "
                              "Load it with `python h1b.py load data/h1b/*.csv`."]
    if refresh and refresh["failed"]:
        result["warnings"] = [f"{f['company']}: {f['error']}" for f in refresh["failed"]]
    return result


_refresh_task: asyncio.Task | None = None


async def _ensure_fresh() -> dict | None:
    """Stale-while-revalidate: a full refresh takes ~80s (Workday is slow), so only the
    very first run (empty cache) blocks. Otherwise answer from the cache right away
    and refresh any stale boards in the background."""
    global _refresh_task
    if catalog.cache_status()["jobs_cached"] == 0:
        return await catalog.refresh()
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(catalog.refresh())
    return None


@mcp.tool()
async def get_job_details(
    job_id: Annotated[str, Field(description="Job id from search_jobs, e.g. 'greenhouse:stripe:8172510'.")],
) -> dict:
    """Full details for one job, including the plain-text job description."""
    job = await catalog.get_job(job_id)
    if job is None:
        return {"error": f"No job with id '{job_id}'. It may have closed; run search_jobs again."}
    return job


@mcp.tool()
async def refresh_jobs(
    force: Annotated[bool, Field(description="Re-fetch every board even if the cache is fresh.")] = False,
) -> dict:
    """Refresh the job cache from the live job boards. Normally automatic; use force=True for the latest data.
    A full refresh can take over a minute (Workday sites are slow)."""
    if _refresh_task is not None and not _refresh_task.done():
        await _refresh_task  # join the in-flight background refresh instead of duplicating it
    result = await catalog.refresh(force=force)
    result.update(catalog.cache_status())
    return result


# ---------------------------------------------------------------- tracker

Status = Literal["saved", "applied", "interviewing", "offer", "rejected", "withdrawn"]


def _safe(fn, *args, **kwargs) -> dict:
    """Turn input errors into a readable tool result instead of a stack trace."""
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def save_job(
    job_id: Annotated[str | None, Field(
        description="Job id from search_jobs. Omit to track a job found elsewhere (then pass company + title).")] = None,
    notes: Annotated[str | None, Field(description="Why you're interested, referral contact, etc.")] = None,
    status: Annotated[Status, Field(description="Starting status; use 'applied' if you already applied.")] = "saved",
    company: Annotated[str | None, Field(description="Manual entry only: company name.")] = None,
    title: Annotated[str | None, Field(description="Manual entry only: job title.")] = None,
    url: Annotated[str | None, Field(description="Manual entry only: posting URL.")] = None,
) -> dict:
    """Add a job to the application tracker. Saving an already-tracked job just appends the note.
    Returns the application, including its numeric id for update_application."""
    return _safe(tracker.save_job, job_id=job_id, notes=notes, status=status,
                 company=company, title=title, url=url)


@mcp.tool()
async def update_application(
    application_id: Annotated[int, Field(description="Numeric application id from save_job or list_applications.")],
    status: Annotated[Status | None, Field(description="New status. Omit to only add a note.")] = None,
    note: Annotated[str | None, Field(
        description="What happened, e.g. 'Recruiter screen scheduled for Tue'. Resets the follow-up clock.")] = None,
) -> dict:
    """Move an application through the pipeline (saved -> applied -> interviewing -> offer/rejected/withdrawn)
    and/or log a note. Every change is kept in the application's history."""
    return _safe(tracker.update_application, application_id, status=status, note=note)


@mcp.tool()
async def list_applications(
    statuses: Annotated[list[Status] | None, Field(description="Only these statuses, e.g. ['applied', 'interviewing'].")] = None,
    company: Annotated[str | None, Field(description="Filter by company name (partial match).")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """View the application tracker, most recently updated first, plus counts per status.
    posting_open=false means the job is no longer listed on the company's board."""
    return _safe(tracker.list_applications, statuses=statuses, company=company, limit=limit)


@mcp.tool()
async def followups_due(
    days: Annotated[int, Field(description="No activity for at least this many days.", ge=1)] = 7,
    statuses: Annotated[list[Status] | None, Field(
        description="Statuses to check. Default: applied and interviewing.")] = None,
) -> dict:
    """Applications that need a follow-up: no status change or note in `days` days. Oldest first."""
    return _safe(tracker.followups_due, days=days, statuses=statuses)


# ---------------------------------------------------------------- H-1B

@mcp.tool()
async def check_sponsorship(
    company: Annotated[str, Field(
        description="Company name, e.g. 'Capital One'. Tracked companies use a vetted match; "
                    "others are fuzzy-matched against USCIS employer names.")],
) -> dict:
    """H-1B sponsorship history from the USCIS H-1B Employer Data Hub: approvals and denials per
    fiscal year (initial = new hires/transfers, continuing = extensions), approval rate, and the
    exact USCIS employer names that were matched so you can sanity-check the match."""
    return _safe(h1b.check_sponsorship, company, catalog.load_companies())


# ---------------------------------------------------------------- prompts

RESUME_PATH = Path(__file__).parent / "data" / "resume.md"  # optional, gitignored


@mcp.prompt(title="Tailor my resume for a job")
async def tailor_resume(job_id: str, resume: str = "") -> str:
    """Pulls in the full job description (and your resume from data/resume.md if you don't
    paste one) and asks for a tailored resume plus a gap analysis."""
    job = await catalog.get_job(job_id)
    if job is None:
        return (f"I wanted to tailor my resume for job '{job_id}', but it isn't in the job cache "
                "(it may have closed). Please run search_jobs to find it again.")

    if not resume.strip() and RESUME_PATH.exists():
        resume = RESUME_PATH.read_text(encoding="utf-8")
    resume_block = (f"<resume>\n{resume.strip()}\n</resume>" if resume.strip()
                    else "I haven't provided my resume yet: ask me to paste it before you start.")

    sponsor_line = ""
    if h1b.has_data():
        s = _safe(h1b.check_sponsorship, job["company"], catalog.load_companies())
        if s.get("found"):
            sponsor_line = (f"\nH-1B context: {job['company']} had {s['recent_initial_approvals']:,} initial "
                            f"H-1B approvals in the last 5 fiscal years.")

    return f"""Tailor my resume for this job.

<job>
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
URL: {job['url']}

{job.get('description') or '(no description available)'}
</job>
{sponsor_line}

{resume_block}

Please:
1. List the 5-8 most important requirements and keywords from the job description.
2. Rewrite my resume bullets to match them. Keep every claim truthful: reorder, reword and
   emphasize what I've actually done, never invent experience, tools or numbers.
3. Suggest a 2-3 line summary for the top of the resume aimed at this role.
4. Point out gaps: requirements I don't clearly meet, and how I could address each one
   honestly (a project, a cover-letter line, or something to learn).
5. After that, offer to save this job to my tracker with save_job if it isn't tracked yet."""


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
