"""Sources against recorded-shape responses (no live calls in CI)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobpilot.sources.adzuna import AdzunaSource
from jobpilot.sources.base import QuotaExceededError, SearchQuery, SourceError, title_matches
from jobpilot.sources.boards import (
    AshbySource,
    Company,
    GreenhouseSource,
    LeverSource,
    load_companies,
)
from jobpilot.sources.http import SourceHttp, SourceLimits
from jobpilot.sources.jsearch import JSearchSource
from jobpilot.sources.registry import build_sources
from jobpilot.sources.remote_boards import RemoteOKSource, RemotiveSource

Q = [SearchQuery("backend developer python", "Hyderabad")]


@pytest.fixture
async def http(sessions):
    client = httpx.AsyncClient()
    h = SourceHttp(client, sessions, backoff_base=0)
    yield h
    await client.aclose()


ADZUNA = {
    "results": [
        {
            "id": "4711",
            "title": "Python Backend Developer",
            "company": {"display_name": "Acme Pvt Ltd"},
            "location": {"display_name": "Hyderabad, Telangana"},
            "description": "FastAPI, PostgreSQL…",
            "redirect_url": "https://www.adzuna.in/land/ad/4711",
            "created": "2026-11-19T10:00:00Z",
            "salary_min": 600000,
            "salary_max": 900000,
            "salary_is_predicted": "1",
            "contract_time": "full_time",
            "category": {"label": "IT Jobs"},
        },
        {"id": "4712", "title": "No link", "company": {}, "location": {}, "redirect_url": None},
    ]
}


async def test_adzuna_normalizes_and_flags_predicted_salary(http) -> None:
    http.register("adzuna", SourceLimits(per_day=10))
    with respx.mock() as router:
        route = router.get("https://api.adzuna.com/v1/api/jobs/in/search/1").respond(
            200, json=ADZUNA
        )
        jobs = await AdzunaSource(http, "id", "key").search(Q)
    [job] = jobs
    assert job.company == "Acme Pvt Ltd" and job.city == "Hyderabad" and job.country == "India"
    assert job.salary_is_estimated and job.currency == "INR"
    assert job.employment_type == "full_time" and job.link_kind == "aggregator"
    assert job.posted_at == datetime(2026, 11, 19, 10, tzinfo=UTC)
    params = route.calls.last.request.url.params
    assert params["what"] == "backend developer python" and params["where"] == "Hyderabad"
    assert params["max_days_old"] == "14"


GREENHOUSE = {
    "jobs": [
        {
            "id": 1,
            "title": "Senior Backend Engineer, Python",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Bengaluru, India"},
            "updated_at": "2026-11-18T08:00:00-05:00",
            "content": "&lt;p&gt;Build &lt;b&gt;FastAPI&lt;/b&gt; services&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Docker&lt;/li&gt;&lt;/ul&gt;",
            "departments": [{"name": "Engineering"}],
        },
        {
            "id": 2,
            "title": "Account Executive",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            "location": {"name": "Mumbai"},
            "updated_at": "2026-11-18T08:00:00Z",
            "content": "",
        },
    ]
}


async def test_greenhouse_unescapes_html_and_filters_titles(http) -> None:
    http.register("greenhouse", SourceLimits())
    with respx.mock() as router:
        router.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
            200, json=GREENHOUSE
        )
        jobs = await GreenhouseSource(http, [Company("Acme", "greenhouse", "acme")]).search(Q)
    [job] = jobs  # the sales role doesn't match the query
    assert "Build FastAPI services" in job.description and "<p>" not in job.description
    assert job.link_kind == "ats" and job.city == "Bengaluru"
    assert job.posted_at == datetime(2026, 11, 18, 13, tzinfo=UTC)


LEVER = [
    {
        "id": "abc",
        "text": "Backend Developer - Python",
        "hostedUrl": "https://jobs.lever.co/zeta/abc",
        "applyUrl": "https://jobs.lever.co/zeta/abc/apply",
        "createdAt": 1763452800000,
        "categories": {"location": "Hyderabad", "commitment": "Full-time", "team": "Eng"},
        "workplaceType": "hybrid",
        "country": "IN",
        "descriptionPlain": "We use Python.",
        "lists": [{"text": "Requirements", "content": "<li>2+ years Python</li>"}],
        "additionalPlain": "",
        "salaryRange": {
            "min": 1200000,
            "max": 1800000,
            "currency": "INR",
            "interval": "per-year-salary",
        },
    }
]


async def test_lever_maps_workplace_salary_and_ms_epoch(http) -> None:
    http.register("lever", SourceLimits())
    with respx.mock() as router:
        router.get("https://api.lever.co/v0/postings/zeta").respond(200, json=LEVER)
        [job] = await LeverSource(http, [Company("Zeta", "lever", "zeta")]).search(Q)
    assert job.remote_type == "hybrid" and job.country == "India"
    assert job.employment_type == "full_time"
    assert job.salary_min == 1200000 and job.currency == "INR"
    assert "2+ years Python" in job.description
    assert job.posted_at is not None and job.posted_at.year == 2025


ASHBY = {
    "jobs": [
        {
            "id": "x1",
            "title": "AI Engineer (Python)",
            "location": "Bengaluru",
            "secondaryLocations": [],
            "employmentType": "FullTime",
            "workplaceType": "OnSite",
            "isRemote": False,
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/sarvam/x1",
            "applyUrl": "https://jobs.ashbyhq.com/sarvam/x1/application",
            "publishedAt": "2026-11-20T00:00:00.000+00:00",
            "descriptionPlain": "LLMs, RAG, Python",
            "compensation": {
                "summaryComponents": [
                    {
                        "compensationType": "Salary",
                        "interval": "1 YEAR",
                        "currencyCode": "INR",
                        "minValue": 2000000,
                        "maxValue": 3000000,
                    }
                ]
            },
        }
    ]
}


async def test_ashby_uses_workplace_type_not_isremote(http) -> None:
    http.register("ashby", SourceLimits())
    with respx.mock() as router:
        route = router.get("https://api.ashbyhq.com/posting-api/job-board/sarvam").respond(
            200, json=ASHBY
        )
        [job] = await AshbySource(http, [Company("Sarvam AI", "ashby", "sarvam")]).search(
            [SearchQuery("AI engineer")]
        )
    assert job.remote_type == "onsite" and job.salary_max == 3000000
    assert job.apply_url.endswith("/application")
    assert route.calls.last.request.url.params["includeCompensation"] == "true"


async def test_jsearch_remotive_remoteok(http) -> None:
    for name in ("jsearch", "remotive", "remoteok"):
        http.register(name, SourceLimits())
    with respx.mock() as router:
        router.get("https://jsearch.p.rapidapi.com/search").respond(
            200,
            json={
                "data": [
                    {
                        "job_id": "j1",
                        "job_title": "Python Developer",
                        "employer_name": "Beta",
                        "job_apply_link": "https://beta.com/careers/1",
                        "job_apply_is_direct": True,
                        "job_city": "Pune",
                        "job_country": "IN",
                        "job_is_remote": False,
                        "job_employment_type": "FULLTIME",
                        "job_posted_at_datetime_utc": "2026-11-20T00:00:00.000Z",
                        "job_description": "Python",
                        "job_min_salary": None,
                    }
                ]
            },
        )
        router.get("https://remotive.com/api/remote-jobs").respond(
            200,
            json={
                "jobs": [
                    {
                        "id": 9,
                        "url": "https://remotive.com/remote-jobs/software-dev/python-9",
                        "title": "Python Backend Developer",
                        "company_name": "Gamma",
                        "job_type": "full_time",
                        "publication_date": "2026-11-19T00:00:00",
                        "candidate_required_location": "Worldwide",
                        "description": "<p>Python</p>",
                    }
                ]
            },
        )
        router.get("https://remoteok.com/api").respond(
            200,
            json=[
                {"legal": "API terms: link back to Remote OK"},
                {
                    "id": "7",
                    "position": "Senior Python Engineer",
                    "company": "Delta",
                    "url": "https://remoteok.com/remote-jobs/7",
                    "location": "USA only",
                    "epoch": 1763600000,
                    "description": "<p>Django</p>",
                    "salary_min": 120000,
                    "salary_max": 150000,
                },
            ],
        )
        js = await JSearchSource(http, "k").search(Q)
        rv = await RemotiveSource(http).search(Q)
        ro = await RemoteOKSource(http).search(Q)
    assert js[0].link_kind == "company" and js[0].country == "India"
    assert rv[0].remote_type == "remote" and rv[0].remote_region == "Worldwide"
    assert len(ro) == 1 and ro[0].remote_region == "USA only" and ro[0].currency == "USD"


async def test_cache_avoids_second_call_and_quota_counts(http) -> None:
    http.register("adzuna", SourceLimits(per_day=1))
    src = AdzunaSource(http, "id", "key")
    with respx.mock() as router:
        route = router.get("https://api.adzuna.com/v1/api/jobs/in/search/1").respond(
            200, json=ADZUNA
        )
        await src.search(Q)
        await src.search(Q)  # identical query: served from cache, no quota used
        assert route.call_count == 1
        assert await http.usage("adzuna") == (1, 1)
        with pytest.raises(QuotaExceededError):
            await src.search([SearchQuery("data analyst")])  # new query, quota spent


async def test_retries_5xx_then_succeeds_and_4xx_fails_fast(http) -> None:
    http.register("greenhouse", SourceLimits())
    with respx.mock() as router:
        flaky = router.get("https://boards-api.greenhouse.io/v1/boards/flaky/jobs").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=GREENHOUSE)]
        )
        router.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs").respond(404)
        src = GreenhouseSource(
            http, [Company("Flaky", "greenhouse", "flaky"), Company("Gone", "greenhouse", "gone")]
        )
        jobs = await src.search(Q)  # one board failing doesn't break the source
        assert flaky.call_count == 2 and len(jobs) == 1
        with pytest.raises(SourceError, match="HTTP 404"):
            await http.get_json(
                "greenhouse", "https://boards-api.greenhouse.io/v1/boards/gone/jobs", cache=False
            )


async def test_all_boards_failing_is_a_source_error(http) -> None:
    http.register("lever", SourceLimits())
    with respx.mock() as router:
        router.get(url__startswith="https://api.lever.co/").respond(500)
        with pytest.raises(SourceError, match="every board failed"):
            await LeverSource(http, [Company("X", "lever", "x")]).search(Q)


def test_title_matching() -> None:
    q = [SearchQuery("backend developer python"), SearchQuery("AI engineer")]
    assert title_matches("Senior Python Engineer", q)
    assert title_matches("Applied AI Engineer", q)
    # a shared role noun alone ("engineer") isn't enough…
    assert not title_matches("Sales Engineer", [SearchQuery("backend engineer")])
    # …but a generic query matches its exact role
    assert title_matches("Software Engineer", [SearchQuery("software engineer")])
    assert not title_matches("Account Executive", q)
    assert not title_matches("Marketing Manager", q)


def test_companies_file_and_registry(make_settings, sessions) -> None:
    companies = load_companies()
    assert len(companies) >= 20
    assert {c.ats for c in companies} == {"greenhouse", "lever", "ashby"}
    settings = make_settings(adzuna_app_id="a", adzuna_app_key="b", enable_remote_boards=True)
    srcs = build_sources(settings, SourceHttp(httpx.AsyncClient(), sessions))
    names = [s.name for s in srcs.sources]
    assert names[:4] == ["adzuna", "greenhouse", "lever", "ashby"]
    assert "jsearch" not in names  # no key → not enabled
    assert [s.name for s in srcs.for_modes(["onsite"])] == names[:4]
