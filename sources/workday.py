"""Workday adapter.

Workday career sites (e.g. https://capitalone.wd12.myworkdayjobs.com/Capital_One)
are backed by an UNOFFICIAL JSON API that their own search page calls:

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs      (listing, 20 per page)
    GET  https://{host}/wday/cxs/{tenant}/{site}{path}     (one job's detail)

It is not a supported public API and may change. Big employers have thousands of
postings, so we never fetch everything: we read the site's location facets, keep
the values matching our target locations (e.g. "Plano, TX"), and query one location
at a time. That also fixes the listing's useless "19 Locations" text — each job is
tagged with the target cities it was found under.

Listings have no description; get_job_details fetches it lazily (fetch_description).

companies.yaml:  board = "<host>/<site>", e.g. "capitalone.wd12.myworkdayjobs.com/Capital_One"
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .base import Job, JobSource, html_to_text, looks_remote

PAGE_SIZE = 20                 # Workday rejects larger pages
MAX_JOBS_PER_COMPANY = 1500    # safety cap
PAGE_CONCURRENCY = 4
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

DEFAULT_LOCATIONS = [
    "Dallas", "Plano", "Irving", "Richardson", "Frisco", "Fort Worth", "Addison",
    "Westlake", "Coppell", "Las Colinas", "Carrollton", "Allen", "McKinney", "DFW",
    "Austin", "Houston", "San Antonio", "Texas", "Remote",
]

_POSTED_RE = re.compile(r"(\d+)\+?\s+days?\s+ago", re.I)


def parse_posted_on(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = _POSTED_RE.search(t)
    if m:
        days = int(m.group(1)) + (1 if "+" in t else 0)  # "30+" -> 31: older than 30
        return now - timedelta(days=days)
    return None


def split_board(board: str) -> tuple[str, str, str]:
    """'capitalone.wd12.myworkdayjobs.com/Capital_One' -> (host, tenant, site)."""
    host, _, site = board.strip("/").partition("/")
    return host, host.split(".")[0], site


class WorkdaySource(JobSource):
    name = "workday"

    async def fetch(self, client: httpx.AsyncClient, board: str, company: str,
                    options: dict | None = None) -> list[Job]:
        host, tenant, site = split_board(board)
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        targets = [t.lower() for t in (options or {}).get("locations") or DEFAULT_LOCATIONS]

        first = await self._page(client, api, {}, 0)
        facet_values = self._matching_location_facets(first.get("facets") or [], targets)

        found: dict[str, dict] = {}       # externalPath -> posting
        cities: dict[str, list[str]] = {}  # externalPath -> matched location descriptors
        sem = asyncio.Semaphore(PAGE_CONCURRENCY)

        async def collect(param: str, value_id: str, descriptor: str):
            facets = {param: [value_id]}
            page0 = await self._page(client, api, facets, 0)
            total = min(page0.get("total") or 0, MAX_JOBS_PER_COMPANY)

            async def page(offset):
                async with sem:
                    return await self._page(client, api, facets, offset)

            pages = [page0] + await asyncio.gather(*(page(o) for o in range(PAGE_SIZE, total, PAGE_SIZE)))
            for p in pages:
                for posting in p.get("jobPostings") or []:
                    path = posting.get("externalPath")
                    if not path:
                        continue
                    found.setdefault(path, posting)
                    cities.setdefault(path, [])
                    if descriptor not in cities[path]:
                        cities[path].append(descriptor)

        for param, value_id, descriptor in facet_values:
            if len(found) >= MAX_JOBS_PER_COMPANY:
                break
            await collect(param, value_id, descriptor)

        return [self._to_job(p, cities[path], host, site, board, company) for path, p in found.items()]

    async def fetch_description(self, client: httpx.AsyncClient, job: dict) -> str | None:
        host, tenant, site = split_board(job["board"])
        path = urlsplit(job["url"]).path
        prefix = f"/{site}"
        if path.startswith(prefix):
            path = path[len(prefix):]
        resp = await client.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}", headers=HEADERS)
        resp.raise_for_status()
        return html_to_text((resp.json().get("jobPostingInfo") or {}).get("jobDescription"))

    # ---------------------------------------------------------------- internals

    @staticmethod
    async def _page(client, api: str, facets: dict, offset: int) -> dict:
        resp = await client.post(api, headers=HEADERS, json={
            "appliedFacets": facets, "limit": PAGE_SIZE, "offset": offset, "searchText": "",
        })
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _matching_location_facets(facets: list[dict], targets: list[str]) -> list[tuple[str, str, str]]:
        """Walk the (possibly nested) facet tree; return (param, id, descriptor) for
        location values whose name contains any target, e.g. 'Plano, TX'."""
        out, seen = [], set()

        def walk(nodes):
            for f in nodes:
                param = f.get("facetParameter") or ""
                for v in f.get("values") or []:
                    if "facetParameter" in v:          # nested group
                        walk([v])
                        continue
                    desc = v.get("descriptor") or ""
                    if ("location" in param.lower()
                            and v.get("id") not in seen
                            and any(re.search(rf"\b{re.escape(t)}\b", desc.lower()) for t in targets)):
                        seen.add(v["id"])
                        out.append((param, v["id"], desc))

        walk(facets)
        # Prefer the specific "locations" facet over country/region groupings.
        out.sort(key=lambda x: (x[0] != "locations", x[2]))
        return out

    def _to_job(self, p: dict, cities: list[str], host: str, site: str, board: str, company: str) -> Job:
        text = p.get("locationsText") or ""
        if cities and re.match(r"^\d+ Locations?$", text):
            location = "; ".join(cities) + f" (+ other locations; {text} total)"
        else:
            location = "; ".join(dict.fromkeys([text] + cities)) if text else "; ".join(cities)
        bullets = p.get("bulletFields") or []
        path = p["externalPath"]
        native = bullets[0] if bullets and len(bullets[0]) <= 24 else path.rsplit("_", 1)[-1]
        title = p.get("title") or ""
        return Job(
            # Short id: "workday:capitalone:R1005333", not the full host/site board string,
            # so people can type it and it costs less context for the AI.
            id=self.make_id(host.split(".")[0], native),
            source=self.name,
            board=board,
            company=company,
            title=title,
            location=location,
            url=f"https://{host}/{site}{path}",
            remote=looks_remote(location, title),
            posted_at=parse_posted_on(p.get("postedOn")),
            description=None,  # fetched lazily by get_job_details
        )
