"""Cheap hard filters, run before any LLM call (spec 8.3). Each drop records a reason."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobpilot.matching.locations import find_city, remote_allows_country
from jobpilot.matching.normalize import (
    JobPosting,
    annualize,
    norm_company,
    required_years,
    title_level,
)
from jobpilot.resume.schema import SENIORITY_ORDER, CandidateProfile
from jobpilot.services.preferences import FeedbackWeights, Preferences

LEVEL_RANK = {lvl: i for i, lvl in enumerate(SENIORITY_ORDER)}  # intern=0 … lead=5
TITLE_RANK = {"intern": 0, "junior": 2, "senior": 4, "lead": 5}


@dataclass
class FilterReport:
    kept: list[JobPosting] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(term.lower())}(?![\w])", text.lower()) is not None


def drop_reason(
    job: JobPosting,
    profile: CandidateProfile,
    prefs: Preferences,
    weights: FeedbackWeights,
    *,
    now: datetime,
    max_age_days: int,
) -> str | None:
    text = f"{job.title}\n{job.description}"
    # freshness
    if job.posted_at is not None and now - job.posted_at > timedelta(days=max_age_days):
        return "stale"
    # employment type (unknown passes)
    if job.employment_type != "unknown" and job.employment_type not in prefs.job_types:
        return "job_type"
    # work mode & location
    mode = job.remote_type
    if mode != "unknown" and mode not in prefs.work_modes:
        return "work_mode"
    if mode == "remote":
        if not remote_allows_country(job.remote_region or job.location, prefs.home_country):
            return "remote_region"
    else:
        wanted = {find_city(loc) or loc.lower() for loc in prefs.locations}
        job_city = job.city or find_city(job.location)
        if job.country and job.country != prefs.home_country and not prefs.abroad_locations():
            return "location"
        if wanted and job_city and job_city not in wanted:
            relocate_ok = prefs.willing_to_relocate and not weights.location_strict
            if not (relocate_ok and (job.country in (None, prefs.home_country))):
                return "location"
    # exclusions
    company = norm_company(job.company)
    for excluded in prefs.excluded_companies:
        if norm_company(excluded) and norm_company(excluded) in company:
            return "excluded_company"
    for kw in prefs.excluded_keywords:
        if _contains(job.title, kw) or _contains(job.description[:2000], kw):
            return "excluded_keyword"
    if prefs.must_have_keywords and not any(_contains(text, kw) for kw in prefs.must_have_keywords):
        return "missing_must_have"
    # seniority far off
    cand = LEVEL_RANK[prefs.experience_level]
    job_level = title_level(job.title)
    gap = 1 if weights.seniority_strict else 2
    if job_level in TITLE_RANK and TITLE_RANK[job_level] - cand > gap:
        return "seniority"
    if job_level == "intern" and cand >= LEVEL_RANK["junior"] + 1:
        return "seniority"
    years = required_years(job.description)
    have = max(profile.total_experience_years, 0.0)
    if years is not None and years > have + (1 if weights.seniority_strict else 2):
        return "seniority"
    # salary below minimum (only when stated, same currency, not a model estimate)
    if (
        prefs.min_salary
        and job.salary_max
        and not job.salary_is_estimated
        and ((job.currency or "").upper() == prefs.min_salary.currency.upper())
    ):
        job_annual = annualize(job.salary_max, job.salary_period)
        if job_annual is not None and job_annual < prefs.min_salary.annual():
            return "salary"
    return None


def apply_filters(
    jobs: list[JobPosting],
    profile: CandidateProfile,
    prefs: Preferences,
    weights: FeedbackWeights,
    *,
    now: datetime,
    max_age_days: int,
) -> FilterReport:
    report = FilterReport()
    for job in jobs:
        reason = drop_reason(job, profile, prefs, weights, now=now, max_age_days=max_age_days)
        if reason:
            report.dropped[reason] += 1
        else:
            report.kept.append(job)
    return report
