"""Ashby adapter.

API: GET https://api.ashbyhq.com/posting-api/job-board/{board}
No auth. Response: {"jobs": [...], "apiVersion": ...}.
Gotcha: Ashby sets isRemote=true on many *Hybrid* jobs, so workplaceType is
the source of truth for remote; isRemote is only a fallback when it's missing.
"""

from __future__ import annotations

import httpx

from .base import Job, JobSource, html_to_text, looks_remote, parse_iso

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"


class AshbySource(JobSource):
    name = "ashby"

    async def fetch(self, client: httpx.AsyncClient, board: str, company: str,
                    options: dict | None = None) -> list[Job]:
        resp = await client.get(API.format(board=board))
        resp.raise_for_status()
        return [
            self._to_job(raw, board, company)
            for raw in resp.json().get("jobs", [])
            if raw.get("isListed", True)
        ]

    def _to_job(self, raw: dict, board: str, company: str) -> Job:
        locations = [raw.get("location") or ""] + [
            s.get("location") or "" for s in (raw.get("secondaryLocations") or [])
        ]
        location = "; ".join(loc for loc in locations if loc)
        title = (raw.get("title") or "").strip()
        workplace = (raw.get("workplaceType") or "").lower()
        if workplace:
            remote = workplace == "remote"
        else:
            remote = bool(raw.get("isRemote")) or looks_remote(location, title)
        return Job(
            id=self.make_id(board, raw["id"]),
            source=self.name,
            board=board,
            company=company,
            title=title,
            location=location,
            url=raw.get("jobUrl") or "",
            remote=remote,
            department=raw.get("department") or raw.get("team"),
            posted_at=parse_iso(raw.get("publishedAt")),
            description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
        )
