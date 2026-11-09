"""Adapter tests against mocked HTTP (respx) — no network. Payload shapes mirror the
real APIs as observed in Nov 2026, including their quirks."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from sources import SOURCES
from sources.base import html_to_text
from sources.workday import parse_posted_on, split_board


async def fetch(source, board, company="Acme", options=None):
    async with httpx.AsyncClient() as client:
        return await SOURCES[source].fetch(client, board, company, options=options)


# ---------------------------------------------------------------- shared helpers

def test_html_to_text_handles_double_escaped_html():
    raw = "&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Go&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(raw) == "About\n\nBuild & ship\n\nGo"


# ---------------------------------------------------------------- Greenhouse

@respx.mock
async def test_greenhouse():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json={
        "jobs": [{
            "id": 42, "title": "Backend Engineer", "company_name": "Acme Inc",
            "location": {"name": "Remote - US"}, "absolute_url": "https://acme.com/jobs/42",
            "first_published": "2026-11-01T10:00:00-04:00", "updated_at": "2026-11-05T10:00:00-04:00",
            "content": "&lt;p&gt;Build APIs&lt;/p&gt;", "departments": [{"name": "Engineering"}],
        }],
    }))
    [job] = await fetch("greenhouse", "acme")
    assert job.id == "greenhouse:acme:42"
    assert job.company == "Acme Inc"
    assert job.remote is True
    assert job.department == "Engineering"
    assert job.description == "Build APIs"
    # first_published (real post date) wins over updated_at
    assert job.posted_at == datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
    assert respx.calls.last.request.url.params["content"] == "true"


@respx.mock
async def test_greenhouse_404_raises():
    respx.get("https://boards-api.greenhouse.io/v1/boards/nope/jobs").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch("greenhouse", "nope")


# ---------------------------------------------------------------- Lever

@respx.mock
async def test_lever():
    respx.get("https://api.lever.co/v0/postings/acme").mock(return_value=httpx.Response(200, json=[{
        "id": "abc-123", "text": "Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "createdAt": 1762000000000, "workplaceType": "hybrid",
        "categories": {"location": "Dallas, TX", "allLocations": ["Dallas, TX", "Austin, TX"], "team": "Infra"},
        "descriptionPlain": "Intro.", "additionalPlain": "Benefits.",
        "lists": [{"text": "What you'll do", "content": "<li>Run Kubernetes</li>"}],
    }]))
    [job] = await fetch("lever", "acme")
    assert job.id == "lever:acme:abc-123"
    assert job.location == "Dallas, TX; Austin, TX"
    assert job.remote is False                     # hybrid is not remote
    assert job.department == "Infra"
    assert job.posted_at == datetime.fromtimestamp(1762000000, tz=timezone.utc)
    assert job.description == "Intro.\n\nWhat you'll do\nRun Kubernetes\n\nBenefits."


# ---------------------------------------------------------------- Ashby

@respx.mock
async def test_ashby_uses_workplace_type_and_skips_unlisted():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(return_value=httpx.Response(200, json={
        "apiVersion": "1",
        "jobs": [
            # Real Ashby quirk: isRemote=true on a Hybrid job.
            {"id": "1", "title": "Data Engineer ", "location": "Austin, Texas", "workplaceType": "Hybrid",
             "isRemote": True, "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/acme/1",
             "publishedAt": "2026-11-02T00:00:00.000+00:00", "descriptionPlain": "Pipelines.",
             "secondaryLocations": [{"location": "Dallas, Texas"}], "department": "Data"},
            {"id": "2", "title": "SRE", "location": "US", "workplaceType": "Remote", "isRemote": True,
             "isListed": True, "jobUrl": "u", "descriptionHtml": "<p>On call</p>"},
            {"id": "3", "title": "Hidden", "location": "US", "isListed": False, "jobUrl": "u"},
        ],
    }))
    jobs = {j.id: j for j in await fetch("ashby", "acme")}
    assert set(jobs) == {"ashby:acme:1", "ashby:acme:2"}
    assert jobs["ashby:acme:1"].remote is False
    assert jobs["ashby:acme:1"].title == "Data Engineer"
    assert jobs["ashby:acme:1"].location == "Austin, Texas; Dallas, Texas"
    assert jobs["ashby:acme:2"].remote is True
    assert jobs["ashby:acme:2"].description == "On call"


# ---------------------------------------------------------------- Workday

WD_BOARD = "acme.wd1.myworkdayjobs.com/Careers"
WD_JOBS = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
FACETS = [{
    "facetParameter": "locationMainGroup",
    "values": [{
        "facetParameter": "locations", "descriptor": "Locations",
        "values": [
            {"descriptor": "Plano, TX", "id": "plano", "count": 45},
            {"descriptor": "Seattle, WA", "id": "seattle", "count": 9},
            {"descriptor": "Dallas, TX", "id": "dallas", "count": 1},
        ],
    }],
}]


def posting(n, locations_text="Plano, TX", posted="Posted 3 Days Ago"):
    return {"title": f"Engineer {n}", "externalPath": f"/job/Plano-TX/Engineer-{n}_R{n}",
            "locationsText": locations_text, "postedOn": posted, "bulletFields": [f"R{n}"]}


def workday_api(request):
    """Fake listing endpoint: 45 Plano jobs (3 pages), 1 Dallas job that is also in Plano."""
    body = json.loads(request.content)
    facets, offset = body["appliedFacets"], body["offset"]
    assert body["limit"] == 20
    if not facets:
        return httpx.Response(200, json={"total": 999, "jobPostings": [], "facets": FACETS})
    if facets == {"locations": ["plano"]}:
        page = [posting(n, "2 Locations" if n == 0 else "Plano, TX") for n in range(offset, min(offset + 20, 45))]
        return httpx.Response(200, json={"total": 45, "jobPostings": page})
    if facets == {"locations": ["dallas"]}:
        return httpx.Response(200, json={"total": 1, "jobPostings": [posting(0, "2 Locations")]})
    raise AssertionError(f"unexpected facets {facets}")  # Seattle must never be queried


@respx.mock
async def test_workday_queries_only_target_locations_and_paginates():
    route = respx.post(WD_JOBS).mock(side_effect=workday_api)
    jobs = {j.id: j for j in await fetch("workday", WD_BOARD)}
    assert len(jobs) == 45                                # job 0 found under both cities, deduped
    multi = jobs[f"workday:{WD_BOARD}:R0"]
    assert multi.location == "Dallas, TX; Plano, TX (+ other locations; 2 Locations total)"
    assert jobs[f"workday:{WD_BOARD}:R1"].location == "Plano, TX"
    assert multi.url == "https://acme.wd1.myworkdayjobs.com/Careers/job/Plano-TX/Engineer-0_R0"
    assert multi.description is None                      # fetched lazily
    # 1 facet discovery + Dallas (1 page) + Plano (3 pages)
    assert route.call_count == 5


@respx.mock
async def test_workday_respects_per_company_locations():
    respx.post(WD_JOBS).mock(side_effect=workday_api)
    jobs = await fetch("workday", WD_BOARD, options={"locations": ["Dallas"]})
    assert [j.id for j in jobs] == [f"workday:{WD_BOARD}:R0"]


@respx.mock
async def test_workday_fetch_description():
    respx.get("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/job/Plano-TX/Engineer-0_R0").mock(
        return_value=httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Ship <b>it</b></p>"}}))
    row = {"board": WD_BOARD, "url": "https://acme.wd1.myworkdayjobs.com/Careers/job/Plano-TX/Engineer-0_R0"}
    async with httpx.AsyncClient() as client:
        assert await SOURCES["workday"].fetch_description(client, row) == "Ship it"


@pytest.mark.parametrize("text, days_ago", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 31),
])
def test_parse_posted_on(text, days_ago):
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    assert parse_posted_on(text, now) == now - timedelta(days=days_ago)


def test_split_board():
    assert split_board("capitalone.wd12.myworkdayjobs.com/Capital_One") == (
        "capitalone.wd12.myworkdayjobs.com", "capitalone", "Capital_One")
