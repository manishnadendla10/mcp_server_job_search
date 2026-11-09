"""Catalog: refresh (retries, failure isolation, cache TTL, closed postings) and search."""

import httpx
import pytest
import respx

import catalog

GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
COMPANIES = [
    {"name": "Good", "source": "greenhouse", "board": "good"},
    {"name": "Flaky", "source": "greenhouse", "board": "flaky"},
    {"name": "Gone", "source": "greenhouse", "board": "gone"},
]


def gh_jobs(*jobs):
    return httpx.Response(200, json={"jobs": [
        {"id": i, "title": t, "location": {"name": loc}, "absolute_url": f"u{i}",
         "first_published": f"{date}T00:00:00Z", "content": "desc"}
        for i, t, loc, date in jobs
    ]})


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(catalog.asyncio, "sleep", instant)


@respx.mock
async def test_refresh_retries_transient_and_isolates_failures():
    respx.get(GH.format("good")).mock(return_value=gh_jobs((1, "Backend Engineer", "Dallas, TX", "2026-11-01")))
    flaky = respx.get(GH.format("flaky")).mock(side_effect=[
        httpx.ConnectError("boom"), httpx.Response(503), gh_jobs((2, "SRE", "Remote", "2026-11-02"))])
    gone = respx.get(GH.format("gone")).mock(return_value=httpx.Response(404))

    result = await catalog.refresh(companies=COMPANIES)

    assert result["refreshed"] == 2
    assert [f["company"] for f in result["failed"]] == ["Gone"]
    assert flaky.call_count == 3          # ConnectError, 503, then success
    assert gone.call_count == 1           # 404 is permanent: no retry
    assert catalog.cache_status()["jobs_cached"] == 2


@respx.mock
async def test_refresh_skips_fresh_boards_unless_forced():
    route = respx.get(GH.format("good")).mock(return_value=gh_jobs((1, "Engineer", "Dallas", "2026-11-01")))
    good = COMPANIES[:1]
    await catalog.refresh(companies=good)
    assert (await catalog.refresh(companies=good))["refreshed"] == 0
    assert route.call_count == 1
    await catalog.refresh(companies=good, force=True)
    assert route.call_count == 2


@respx.mock
async def test_refresh_drops_closed_postings():
    respx.get(GH.format("good")).mock(side_effect=[
        gh_jobs((1, "A", "Dallas", "2026-11-01"), (2, "B", "Dallas", "2026-11-01")),
        gh_jobs((2, "B", "Dallas", "2026-11-01")),
    ])
    await catalog.refresh(companies=COMPANIES[:1])
    await catalog.refresh(companies=COMPANIES[:1], force=True)
    assert await catalog.get_job("greenhouse:good:1") is None
    assert (await catalog.get_job("greenhouse:good:2"))["title"] == "B"


@respx.mock
async def test_search_filters_and_ordering():
    respx.get(GH.format("good")).mock(return_value=gh_jobs(
        (1, "Senior Backend Engineer", "Dallas, TX", "2026-10-01"),
        (2, "Cloud Platform Engineer", "Remote - US", "2099-01-01"),   # far future = always "recent"
        (3, "Account Executive", "Dallas, TX", "2099-01-01"),
    ))
    await catalog.refresh(companies=COMPANIES[:1])

    r = catalog.search(keywords=["backend", "cloud"])
    assert [j["id"] for j in r["jobs"]] == ["greenhouse:good:2", "greenhouse:good:1"]  # newest first

    assert [j["id"] for j in catalog.search(locations=["dallas"], keywords=["engineer"])["jobs"]] == ["greenhouse:good:1"]
    assert [j["id"] for j in catalog.search(remote=True)["jobs"]] == ["greenhouse:good:2"]
    assert {j["id"] for j in catalog.search(posted_within_days=7)["jobs"]} == {"greenhouse:good:2", "greenhouse:good:3"}

    limited = catalog.search(limit=1)
    assert limited["total_matches"] == 3 and limited["returned"] == 1
