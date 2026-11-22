"""Adzuna (developer.adzuna.com): keyword search, India via /jobs/in/search/{page}.

Terms: personal job search is a permitted use; ads must be attributed to Adzuna with a
link (we show "via Adzuna" linking to the local Adzuna site). Descriptions are snippets
and salaries are often model-predicted (`salary_is_predicted`), so predicted salaries
are flagged as estimates and never used to filter jobs out.
"""

from __future__ import annotations

import asyncio
from typing import Any

from jobpilot.matching.normalize import JobPosting, employment_type, parse_datetime, place_fields
from jobpilot.sources.base import SearchQuery, SourceError, clean_company
from jobpilot.sources.http import SourceHttp

BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
CREDIT = {
    "in": "https://www.adzuna.in",
    "gb": "https://www.adzuna.co.uk",
    "us": "https://www.adzuna.com",
}
CURRENCY = {
    "in": "INR",
    "gb": "GBP",
    "us": "USD",
    "de": "EUR",
    "fr": "EUR",
    "nl": "EUR",
    "ca": "CAD",
    "au": "AUD",
    "sg": "SGD",
}


class AdzunaSource:
    name = "adzuna"
    label = "Adzuna"

    def __init__(
        self,
        http: SourceHttp,
        app_id: str,
        app_key: str,
        *,
        country: str = "in",
        results_per_page: int = 50,
        max_days_old: int = 14,
    ) -> None:
        self._http = http
        self._id, self._key = app_id, app_key
        self._country = country
        self._per_page = results_per_page
        self._max_days = max_days_old
        self.credit_url = CREDIT.get(country, "https://www.adzuna.com")

    async def _one(self, q: SearchQuery) -> list[JobPosting]:
        params: dict[str, Any] = {
            "app_id": self._id,
            "app_key": self._key,
            "what": q.keywords,
            "results_per_page": self._per_page,
            "max_days_old": self._max_days,
            "content-type": "application/json",
        }
        if q.location:
            params["where"] = q.location
        if q.remote:
            params["what_or"] = "remote"
        data = await self._http.get_json(
            self.name, BASE.format(country=self._country, page=1), params=params
        )
        return [self.normalize(r) for r in data.get("results", []) if r.get("redirect_url")]

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]:
        results = await asyncio.gather(*(self._one(q) for q in queries), return_exceptions=True)
        jobs: list[JobPosting] = []
        errors = [r for r in results if isinstance(r, BaseException)]
        for r in results:
            if not isinstance(r, BaseException):
                jobs.extend(r)
        if errors and not jobs:
            raise errors[0] if isinstance(errors[0], SourceError) else SourceError(str(errors[0]))
        return jobs

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        loc = (raw.get("location") or {}).get("display_name")
        title = raw.get("title") or ""
        description = raw.get("description") or ""
        predicted = str(raw.get("salary_is_predicted", "0")) == "1"
        return JobPosting(
            source=self.name,
            source_job_id=str(raw.get("id")),
            title=title,
            company=clean_company(
                (raw.get("company") or {}).get("display_name"), "Unknown company"
            ),
            location=loc,
            **place_fields(loc, title, description[:300]),
            employment_type=employment_type(
                raw.get("contract_time"), raw.get("contract_type"), title=title
            ),
            salary_min=raw.get("salary_min"),
            salary_max=raw.get("salary_max"),
            currency=CURRENCY.get(self._country) if raw.get("salary_min") else None,
            salary_period="year" if raw.get("salary_min") else None,
            salary_is_estimated=predicted,
            description=description,
            apply_url=raw["redirect_url"],
            link_kind="aggregator",
            posted_at=parse_datetime(raw.get("created")),
            raw={"category": (raw.get("category") or {}).get("label")},
        )
