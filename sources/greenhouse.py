"""Greenhouse adapter.

API: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
No auth. `content=true` includes the (HTML-escaped) job description, so one
request per company gives us everything — no per-job detail calls needed.
"""

from __future__ import annotations

import httpx

from .base import Job, JobSource, html_to_text, looks_remote, parse_iso

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


class GreenhouseSource(JobSource):
    name = "greenhouse"

    async def fetch(self, client: httpx.AsyncClient, board: str, company: str,
                    options: dict | None = None) -> list[Job]:
        resp = await client.get(API.format(board=board), params={"content": "true"})
        resp.raise_for_status()
        return [self._to_job(raw, board, company) for raw in resp.json().get("jobs", [])]

    def _to_job(self, raw: dict, board: str, company: str) -> Job:
        location = (raw.get("location") or {}).get("name") or ""
        departments = raw.get("departments") or []
        title = raw.get("title") or ""
        return Job(
            id=self.make_id(board, raw["id"]),
            source=self.name,
            board=board,
            company=raw.get("company_name") or company,
            title=title,
            location=location,
            url=raw.get("absolute_url") or "",
            remote=looks_remote(location, title),
            department=departments[0]["name"] if departments else None,
            # first_published = real post date; updated_at changes on every edit
            posted_at=parse_iso(raw.get("first_published")) or parse_iso(raw.get("updated_at")),
            description=html_to_text(raw.get("content")),
        )
