"""JSearch on RapidAPI (aggregates Google for Jobs). Tiny free quota: used sparingly."""

from __future__ import annotations

from typing import Any

from jobpilot.matching.normalize import (
    JobPosting,
    employment_type,
    parse_datetime,
    place_fields,
    salary_period,
)
from jobpilot.sources.base import SearchQuery, clean_company
from jobpilot.sources.http import SourceHttp

URL = "https://jsearch.p.rapidapi.com/search"
HOST = "jsearch.p.rapidapi.com"


class JSearchSource:
    name = "jsearch"
    label = "JSearch"
    credit_url = None

    def __init__(
        self, http: SourceHttp, api_key: str, *, country: str = "in", max_queries: int = 2
    ) -> None:
        self._http = http
        self._key = api_key
        self._country = country
        self._max_queries = max_queries

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for q in queries[: self._max_queries]:  # strictly rationed: free plan is tiny
            where = q.location or ("remote" if q.remote else "India")
            data = await self._http.get_json(
                self.name,
                URL,
                params={
                    "query": f"{q.keywords} in {where}",
                    "page": 1,
                    "num_pages": 1,
                    "country": self._country,
                    "date_posted": "week",
                },
                headers={"X-RapidAPI-Key": self._key, "X-RapidAPI-Host": HOST},
            )
            jobs.extend(self.normalize(r) for r in data.get("data", []) if r.get("job_apply_link"))
        return jobs

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        loc_parts = [raw.get("job_city"), raw.get("job_state"), raw.get("job_country")]
        loc = ", ".join(p for p in loc_parts if p) or None
        title = raw.get("job_title") or ""
        fields = place_fields(loc, title)
        if raw.get("job_is_remote"):
            fields["remote_type"] = "remote"
        if raw.get("job_country") == "IN":
            fields["country"] = "India"
        return JobPosting(
            source=self.name,
            source_job_id=str(raw.get("job_id")),
            title=title,
            company=clean_company(raw.get("employer_name"), "Unknown company"),
            location=loc,
            **fields,
            employment_type=employment_type(raw.get("job_employment_type"), title=title),
            salary_min=raw.get("job_min_salary"),
            salary_max=raw.get("job_max_salary"),
            currency=raw.get("job_salary_currency"),
            salary_period=salary_period(raw.get("job_salary_period")),
            description=raw.get("job_description") or "",
            apply_url=raw["job_apply_link"],
            link_kind="company" if raw.get("job_apply_is_direct") else "aggregator",
            posted_at=parse_datetime(raw.get("job_posted_at_datetime_utc")),
            raw={"publisher": raw.get("job_publisher")},
        )
