"""Company ATS boards: Greenhouse, Lever, Ashby (public, no key).

These give direct company apply links, the highest-quality links (spec 7). They have
no server-side search, so each board is fetched once (cached ~6h) and filtered locally
by title against the run's queries. Companies come from an editable `companies.yaml`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobpilot.core.logging import get_logger
from jobpilot.matching.normalize import (
    JobPosting,
    employment_type,
    html_to_text,
    parse_datetime,
    place_fields,
    salary_period,
)
from jobpilot.sources.base import SearchQuery, SourceError, title_matches
from jobpilot.sources.http import SourceHttp

log = get_logger(__name__)
BUNDLED_COMPANIES = Path(__file__).resolve().parents[1] / "data" / "companies.yaml"


@dataclass(frozen=True, slots=True)
class Company:
    name: str
    ats: str  # greenhouse | lever | ashby
    token: str
    region: str = "global"  # lever: "global" or "eu"


def load_companies(path: Path | None = None) -> list[Company]:
    data = yaml.safe_load((path or BUNDLED_COMPANIES).read_text(encoding="utf-8")) or {}
    out = []
    for item in data.get("companies", []):
        if item.get("enabled", True):
            out.append(
                Company(
                    item["name"], item["ats"].lower(), item["token"], item.get("region", "global")
                )
            )
    return out


class _BoardSource:
    name = "board"
    label = "Company boards"
    credit_url = None

    def __init__(self, http: SourceHttp, companies: list[Company]) -> None:
        self._http = http
        self._companies = [c for c in companies if c.ats == self.name]

    @property
    def companies(self) -> list[Company]:
        return self._companies

    async def fetch_board(self, company: Company) -> list[JobPosting]:
        raise NotImplementedError

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]:
        results = await asyncio.gather(
            *(self.fetch_board(c) for c in self._companies), return_exceptions=True
        )
        jobs: list[JobPosting] = []
        failures = 0
        for company, result in zip(self._companies, results, strict=True):
            if isinstance(result, BaseException):
                failures += 1
                log.warning(
                    "board.failed", source=self.name, company=company.name, error=str(result)
                )
                continue
            jobs.extend(j for j in result if title_matches(j.title, queries))
        if self._companies and failures == len(self._companies):
            raise SourceError(f"{self.name}: every board failed")
        return jobs


class GreenhouseSource(_BoardSource):
    name = "greenhouse"
    label = "Greenhouse"

    async def fetch_board(self, company: Company) -> list[JobPosting]:
        data = await self._http.get_json(
            self.name,
            f"https://boards-api.greenhouse.io/v1/boards/{company.token}/jobs",
            params={"content": "true"},
        )
        return [self.normalize(j, company) for j in data.get("jobs", []) if j.get("absolute_url")]

    def normalize(self, raw: dict[str, Any], company: Company) -> JobPosting:
        loc = (raw.get("location") or {}).get("name")
        title = raw.get("title") or ""
        description = html_to_text(raw.get("content"))
        return JobPosting(
            source=self.name,
            source_job_id=f"{company.token}:{raw.get('id')}",
            title=title,
            company=raw.get("company_name") or company.name,
            location=loc,
            **place_fields(loc, title),
            employment_type=employment_type(title=title),
            description=description,
            apply_url=raw["absolute_url"],
            link_kind="ats",
            # The list endpoint has `updated_at` only; `first_published` is per-job.
            posted_at=parse_datetime(raw.get("first_published") or raw.get("updated_at")),
            raw={"departments": [d.get("name") for d in raw.get("departments") or []]},
        )


class LeverSource(_BoardSource):
    name = "lever"
    label = "Lever"

    async def fetch_board(self, company: Company) -> list[JobPosting]:
        host = "api.eu.lever.co" if company.region == "eu" else "api.lever.co"
        data = await self._http.get_json(
            self.name, f"https://{host}/v0/postings/{company.token}", params={"mode": "json"}
        )
        items = data if isinstance(data, list) else []
        return [self.normalize(j, company) for j in items if j.get("hostedUrl")]

    def normalize(self, raw: dict[str, Any], company: Company) -> JobPosting:
        cats = raw.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or []) or None
        title = raw.get("text") or ""
        parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
        for block in raw.get("lists") or []:
            parts.append(f"{block.get('text', '')}\n{html_to_text(block.get('content'))}")
        parts.append(raw.get("additionalPlain") or "")
        fields = place_fields(loc, title)
        workplace = (raw.get("workplaceType") or "").lower()
        if workplace in {"remote", "hybrid"}:
            fields["remote_type"] = workplace
        elif workplace == "on-site":
            fields["remote_type"] = "onsite"
        if raw.get("country") == "IN":
            fields["country"] = "India"
        salary = raw.get("salaryRange") or {}
        return JobPosting(
            source=self.name,
            source_job_id=f"{company.token}:{raw.get('id')}",
            title=title,
            company=company.name,
            location=loc,
            **fields,
            employment_type=employment_type(cats.get("commitment"), title=title),
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            currency=salary.get("currency"),
            salary_period=salary_period((salary.get("interval") or "").replace("per-", "")),
            description="\n\n".join(p for p in parts if p).strip(),
            apply_url=raw["hostedUrl"],
            link_kind="ats",
            posted_at=parse_datetime(raw.get("createdAt")),
            raw={"team": cats.get("team")},
        )


class AshbySource(_BoardSource):
    name = "ashby"
    label = "Ashby"

    async def fetch_board(self, company: Company) -> list[JobPosting]:
        data = await self._http.get_json(
            self.name,
            f"https://api.ashbyhq.com/posting-api/job-board/{company.token}",
            params={"includeCompensation": "true"},
        )
        return [
            self.normalize(j, company)
            for j in data.get("jobs", [])
            if (j.get("jobUrl") or j.get("applyUrl")) and j.get("isListed", True)
        ]

    def normalize(self, raw: dict[str, Any], company: Company) -> JobPosting:
        locs = [raw.get("location")] + [
            s.get("location") for s in raw.get("secondaryLocations") or []
        ]
        loc = ", ".join(x for x in locs if x) or None
        title = raw.get("title") or ""
        fields = place_fields(loc, title)
        workplace = (raw.get("workplaceType") or "").lower()
        fields["remote_type"] = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(
            workplace, fields["remote_type"]
        )
        comp = _ashby_salary(raw.get("compensation") or {})
        return JobPosting(
            source=self.name,
            source_job_id=f"{company.token}:{raw.get('id')}",
            title=title,
            company=company.name,
            location=loc,
            **fields,
            employment_type=employment_type(raw.get("employmentType"), title=title),
            **comp,
            description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
            apply_url=raw.get("applyUrl") or raw["jobUrl"],
            link_kind="ats",
            posted_at=parse_datetime(raw.get("publishedAt")),
        )


def _ashby_salary(comp: dict[str, Any]) -> dict[str, Any]:
    for part in comp.get("summaryComponents") or []:
        if (part.get("compensationType") or "").lower() == "salary":
            return {
                "salary_min": part.get("minValue"),
                "salary_max": part.get("maxValue"),
                "currency": part.get("currencyCode"),
                "salary_period": salary_period((part.get("interval") or "").replace("1 ", "")),
            }
    return {}
