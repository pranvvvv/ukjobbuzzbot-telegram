"""Remote-job boards with public APIs: Remotive and Remote OK.

Terms (verified 2026-11): both require crediting them and linking back to their job
URL, so the apply link IS their listing page and cards say "via Remotive/Remote OK".
Remotive delays jobs by 24h and forbids redistributing listings to other job sites.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from jobpilot.matching.normalize import (
    JobPosting,
    employment_type,
    html_to_text,
    parse_datetime,
)
from jobpilot.sources.base import SearchQuery, title_matches
from jobpilot.sources.http import SourceHttp


class RemotiveSource:
    name = "remotive"
    label = "Remotive"
    credit_url = "https://remotive.com"

    def __init__(self, http: SourceHttp, *, max_queries: int = 3) -> None:
        self._http = http
        self._max = max_queries

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]:
        results = await asyncio.gather(
            *(
                self._http.get_json(
                    self.name,
                    "https://remotive.com/api/remote-jobs",
                    params={"search": q.keywords, "limit": 50},
                )
                for q in queries[: self._max]
            )
        )
        jobs = [self.normalize(j) for data in results for j in data.get("jobs", []) if j.get("url")]
        return [j for j in jobs if title_matches(j.title, queries)]

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        title = raw.get("title") or ""
        return JobPosting(
            source=self.name,
            source_job_id=str(raw.get("id")),
            title=title,
            company=raw.get("company_name") or "Unknown company",
            location=raw.get("candidate_required_location"),
            remote_type="remote",
            remote_region=raw.get("candidate_required_location"),
            employment_type=employment_type(raw.get("job_type"), title=title),
            description=html_to_text(raw.get("description")),
            apply_url=raw["url"],
            link_kind="aggregator",
            posted_at=parse_datetime(raw.get("publication_date")),
            raw={"salary_text": raw.get("salary")},
        )


class RemoteOKSource:
    name = "remoteok"
    label = "Remote OK"
    credit_url = "https://remoteok.com"

    def __init__(self, http: SourceHttp) -> None:
        self._http = http

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]:
        data = await self._http.get_json(
            self.name, "https://remoteok.com/api", cache_ttl=timedelta(hours=6)
        )
        items = (
            [d for d in data if isinstance(d, dict) and d.get("id")]
            if isinstance(data, list)
            else []
        )
        jobs = [self.normalize(j) for j in items if j.get("url")]
        return [j for j in jobs if title_matches(j.title, queries)]

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        title = raw.get("position") or ""
        return JobPosting(
            source=self.name,
            source_job_id=str(raw.get("id")),
            title=title,
            company=raw.get("company") or "Unknown company",
            location=raw.get("location") or "Remote",
            remote_type="remote",
            remote_region=raw.get("location"),
            employment_type=employment_type(title=title),
            salary_min=raw.get("salary_min") or None,
            salary_max=raw.get("salary_max") or None,
            currency="USD" if raw.get("salary_min") else None,
            salary_period="year" if raw.get("salary_min") else None,
            description=html_to_text(raw.get("description")),
            apply_url=raw["url"],  # their terms: link back to the Remote OK listing
            link_kind="aggregator",
            posted_at=parse_datetime(raw.get("epoch") or raw.get("date")),
            raw={"tags": raw.get("tags") or []},
        )
