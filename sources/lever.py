"""Lever adapter.

API: GET https://api.lever.co/v0/postings/{board}?mode=json
No auth, returns every posting in one response (a bare JSON list).
`createdAt` is epoch milliseconds. Descriptions come split into an intro,
a list of sections ("What You'll Do", "Qualifications", ...) and a closing part.
"""

from __future__ import annotations

import httpx

from .base import Job, JobSource, html_to_text, looks_remote, parse_epoch_ms

API = "https://api.lever.co/v0/postings/{board}"


class LeverSource(JobSource):
    name = "lever"

    async def fetch(self, client: httpx.AsyncClient, board: str, company: str,
                    options: dict | None = None) -> list[Job]:
        resp = await client.get(API.format(board=board), params={"mode": "json"})
        resp.raise_for_status()
        return [self._to_job(raw, board, company) for raw in resp.json()]

    def _to_job(self, raw: dict, board: str, company: str) -> Job:
        cats = raw.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        location = "; ".join(locations)
        title = raw.get("text") or ""
        workplace = (raw.get("workplaceType") or "").lower()
        return Job(
            id=self.make_id(board, raw["id"]),
            source=self.name,
            board=board,
            company=company,
            title=title,
            location=location,
            url=raw.get("hostedUrl") or "",
            remote=workplace == "remote" or (not workplace and looks_remote(location, title)),
            department=cats.get("department") or cats.get("team"),
            posted_at=parse_epoch_ms(raw.get("createdAt")),
            description=self._description(raw),
        )

    @staticmethod
    def _description(raw: dict) -> str:
        parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
        for section in raw.get("lists") or []:
            body = html_to_text(section.get("content"))
            if body:
                parts.append(f"{section.get('text', '').strip()}\n{body}")
        parts.append(raw.get("additionalPlain") or html_to_text(raw.get("additional")))
        return "\n\n".join(p.strip() for p in parts if p and p.strip())
