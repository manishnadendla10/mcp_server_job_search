"""Command-line interface: the same features as the MCP server, for people.

    python jobsearch.py search backend cloud -l Dallas Plano --days 7 --sponsors-only
    python jobsearch.py show greenhouse:stripe:8172510
    python jobsearch.py save greenhouse:stripe:8172510 --note "referral from Sam"
    python jobsearch.py update 3 --status applied --note "applied on site"
    python jobsearch.py list --status applied interviewing
    python jobsearch.py followups --days 7
    python jobsearch.py sponsor "Capital One"
    python jobsearch.py refresh --force

Add --json to search/show/list/followups/sponsor for machine-readable output.
Calls the same catalog / tracker / h1b functions as server.py, so the two never drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import textwrap

import catalog
import h1b
import tracker

log = logging.getLogger("jobsearch")


# ---------------------------------------------------------------- output helpers

def _width() -> int:
    return max(80, shutil.get_terminal_size((120, 30)).columns)


def _cut(text, width: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= width else s[: max(1, width - 1)] + "…"


def table(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    """columns: (key, header, width). width 0 = take the remaining space;
    width -1 = never truncate (sized to its longest value) — used for ids people copy."""
    widths = []
    for key, header, w in columns:
        widths.append(max([len(header)] + [len(str(r.get(key) or "")) for r in rows]) if w == -1 else w)
    fixed = sum(w for w in widths if w > 0) + 2 * (len(columns) - 1)
    flex = max(15, _width() - fixed)
    widths = [w if w else flex for w in widths]
    print("  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(_cut(r.get(k), w).ljust(w) for (k, _, _), w in zip(columns, widths)).rstrip())


def dump(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


async def _refresh(force: bool = False, quiet: bool = False) -> None:
    """Fetch stale boards. Unlike the server, the CLI just waits (with a heads-up)."""
    stale = catalog.stale_count(force)
    if not stale:
        return
    if not quiet:
        print(f"Refreshing {stale} job boards (first run can take ~1-2 min)...", file=sys.stderr)
    result = await catalog.refresh(force=force)
    if not quiet:
        print(f"  refreshed {result['refreshed']} in {result['seconds']}s", file=sys.stderr)
    for f in result["failed"]:
        print(f"  warning: {f['company']}: {f['error']}", file=sys.stderr)


# ---------------------------------------------------------------- commands

async def cmd_search(a) -> int:
    if not a.no_refresh:
        await _refresh(quiet=a.json)
    r = catalog.search(
        keywords=a.keywords, locations=a.location, remote=a.remote, posted_within_days=a.days,
        companies=a.company, search_descriptions=a.desc, limit=a.limit,
        sponsors_only=a.sponsors_only, min_h1b_approvals=a.min_h1b,
    )
    if a.sponsors_only and not h1b.has_data():
        print("warning: no H-1B data loaded, so --sponsors-only matches nothing. "
              "Run: python h1b.py load data/h1b/*.csv", file=sys.stderr)
    if a.json:
        dump(r)
        return 0
    if not r["jobs"]:
        print("No matching jobs.")
        return 0
    has_h1b = any("h1b_approvals_5y" in j for j in r["jobs"])
    cols = [("posted", "POSTED", 10), ("company", "COMPANY", 16), ("title", "TITLE", 0), ("location", "LOCATION", 26)]
    if has_h1b:
        cols.append(("h1b", "H-1B 5Y", 7))
    cols.append(("id", "ID", -1))
    rows = [{**j, "h1b": f"{j['h1b_approvals_5y']:,}" if "h1b_approvals_5y" in j else ""} for j in r["jobs"]]
    table(rows, cols)
    more = r["total_matches"] - r["returned"]
    print(f"\n{r['total_matches']} matches" + (f" (showing {r['returned']}; use --limit for more)" if more else ""))
    return 0


async def cmd_show(a) -> int:
    job = await catalog.get_job(a.job_id)
    if job is None:
        return fail(f"no job with id '{a.job_id}' (it may have closed; search again)")
    if a.json:
        dump(job)
        return 0
    print(job["title"])
    print(f"{job['company']}  |  {job['location']}" + ("  |  remote" if job["remote"] else ""))
    print(f"Posted: {(job.get('posted_at') or '?')[:10]}   Id: {job['id']}")
    print(f"Apply:  {job['url']}\n")
    width = min(_width(), 100)
    for para in (job.get("description") or "(no description)").split("\n"):
        print(textwrap.fill(para, width) if para.strip() else "")
    return 0


async def cmd_refresh(a) -> int:
    await _refresh(force=a.force)
    s = catalog.cache_status()
    print(f"{s['jobs_cached']:,} jobs cached from {s['boards_cached']} boards (oldest fetch {s['oldest_fetch']})")
    return 0


def _print_app(app: dict) -> None:
    print(f"#{app['id']}  {app['title']} @ {app['company']}  [{app['status']}]")
    if app.get("url"):
        print(f"    {app['url']}")
    for h in app.get("history", []):
        note = f" - {h['note']}" if h.get("note") else ""
        print(f"    {h['created_at'][:10]}  {h['status']}{note}")


def cmd_save(a) -> int:
    try:
        app = tracker.save_job(job_id=a.job_id, notes=a.note, status=a.status,
                               company=a.company, title=a.title, url=a.url)
    except ValueError as e:
        return fail(str(e))
    print("Already tracked; note added." if app.get("already_tracked") else "Saved.")
    _print_app(app)
    return 0


def cmd_update(a) -> int:
    try:
        app = tracker.update_application(a.application_id, status=a.status, note=a.note)
    except ValueError as e:
        return fail(str(e))
    print("Updated.")
    _print_app(app)
    return 0


def _app_rows(apps: list[dict]) -> list[dict]:
    return [{**x, "id": f"#{x['id']}", "days": x["days_since_update"],
             "open": "" if x["posting_open"] or not x.get("job_id") else "closed"} for x in apps]


APP_COLUMNS = [("id", "ID", 5), ("status", "STATUS", 12), ("company", "COMPANY", 16),
               ("title", "TITLE", 0), ("days", "IDLE DAYS", 9), ("open", "POSTING", 7)]


def cmd_list(a) -> int:
    try:
        r = tracker.list_applications(statuses=a.status, company=a.company, limit=a.limit)
    except ValueError as e:
        return fail(str(e))
    if a.json:
        dump(r)
        return 0
    counts = "  ".join(f"{s}: {n}" for s, n in r["counts_by_status"].items() if n)
    if not r["applications"]:
        print("No applications" + (" match." if (a.status or a.company) else " yet. Save one with: jobsearch.py save <job id>"))
        return 0
    table(_app_rows(r["applications"]), APP_COLUMNS)
    print(f"\n{counts}")
    return 0


def cmd_followups(a) -> int:
    try:
        r = tracker.followups_due(days=a.days, statuses=a.status)
    except ValueError as e:
        return fail(str(e))
    if a.json:
        dump(r)
        return 0
    if not r["due"]:
        checked = "/".join(r["statuses_checked"])
        counts = tracker.list_applications(limit=1)["counts_by_status"]
        if not any(counts[s] for s in r["statuses_checked"]):
            print(f"Nothing to follow up on: you have no {checked} applications.")
        else:
            print(f"Nothing to follow up on: every {checked} application "
                  f"has had activity in the last {a.days} days.")
        return 0
    print(f"{len(r['due'])} application(s) with no activity in {a.days}+ days:\n")
    table(_app_rows(r["due"]), APP_COLUMNS)
    print('\nLog a follow-up with: jobsearch.py update <id> --note "emailed recruiter"')
    return 0


def cmd_sponsor(a) -> int:
    try:
        r = h1b.check_sponsorship(a.company, catalog.load_companies())
    except ValueError as e:
        return fail(str(e))
    if a.json:
        dump(r)
        return 0
    if not r["found"]:
        print(f"{r['company']}: {r['message']}")
        return 0
    print(f"{r['company']}  ({r['match']})")
    print(f"Last {h1b.RECENT_YEARS} fiscal years: {r['recent_initial_approvals']:,} initial approvals, "
          f"{r['recent_total_approvals']:,} total\n")
    rows = [{**y, "rate": f"{y['approval_rate']:.1%}" if y["approval_rate"] is not None else "-",
             **{k: f"{y[k]:,}" for k in ("initial_approvals", "initial_denials", "continuing_approvals")}}
            for y in r["by_fiscal_year"]]
    table(rows, [("fiscal_year", "FY", 6), ("initial_approvals", "INITIAL", 9), ("initial_denials", "DENIED", 8),
                 ("continuing_approvals", "CONTINUING", 10), ("rate", "APPROVAL RATE", 13)])
    print("\nMatched USCIS employers:")
    for e in r["matched_uscis_employers"]:
        print(f"  {e['approvals']:>7,}  {e['employer']}")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobsearch", description="Search job boards, track applications, check H-1B sponsorship.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'jobsearch.py <command> -h' for a command's options.")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    s = sub.add_parser("search", help="search open jobs (newest first)")
    s.add_argument("keywords", nargs="*", help="match ANY of these in title/department, e.g. backend cloud")
    s.add_argument("-l", "--location", nargs="+", metavar="PLACE", help="match ANY, e.g. Dallas Plano Remote")
    s.add_argument("-c", "--company", nargs="+", metavar="NAME", help="only these companies")
    s.add_argument("-d", "--days", type=int, metavar="N", help="posted within N days")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--remote", dest="remote", action="store_true", default=None, help="remote jobs only")
    g.add_argument("--no-remote", dest="remote", action="store_false", help="exclude remote jobs")
    s.add_argument("--sponsors-only", action="store_true", help="only companies with H-1B approvals (last 5 FY)")
    s.add_argument("--min-h1b", type=int, default=1, metavar="N", help="with --sponsors-only: at least N approvals")
    s.add_argument("--desc", action="store_true", help="also match keywords in job descriptions")
    s.add_argument("-n", "--limit", type=int, default=25, help="max rows (default 25, max 100)")
    s.add_argument("--no-refresh", action="store_true", help="use cached jobs only, never fetch")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("show", help="full details for one job")
    s.add_argument("job_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("refresh", help="fetch stale job boards now")
    s.add_argument("--force", action="store_true", help="re-fetch every board")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("save", help="track a job (by id, or --company/--title for a manual entry)")
    s.add_argument("job_id", nargs="?")
    s.add_argument("--note")
    s.add_argument("--status", default="saved", choices=tracker.STATUSES)
    s.add_argument("--company", help="manual entry")
    s.add_argument("--title", help="manual entry")
    s.add_argument("--url", help="manual entry")
    s.set_defaults(func=cmd_save)

    s = sub.add_parser("update", help="change an application's status and/or add a note")
    s.add_argument("application_id", type=int)
    s.add_argument("--status", choices=tracker.STATUSES)
    s.add_argument("--note")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("list", help="show tracked applications")
    s.add_argument("--status", nargs="+", choices=tracker.STATUSES)
    s.add_argument("--company")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("followups", help="applications with no activity in N days")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--status", nargs="+", choices=tracker.STATUSES, help="default: applied interviewing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_followups)

    s = sub.add_parser("sponsor", help="a company's H-1B history")
    s.add_argument("company")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sponsor)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(message)s")
    for stream in (sys.stdout, sys.stderr):
        try:
            # Real consoles get Unicode right; pipes/redirects on Windows default to the legacy
            # code page and mangle job titles ("–", "…"), so use UTF-8 there.
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    result = args.func(args)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


if __name__ == "__main__":
    sys.exit(main())
