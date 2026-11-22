"""Pluggable job-source interface (spec 7). Adding a source = one file + one registry line."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rapidfuzz import fuzz

from jobpilot.matching.normalize import JobPosting, norm_title


@dataclass(frozen=True, slots=True)
class SearchQuery:
    keywords: str
    location: str | None = None  # a city; None = nationwide/remote
    remote: bool = False


@dataclass(slots=True)
class SourceResult:
    source: str
    jobs: list[JobPosting] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None  # e.g. "daily quota reached"
    calls: int = 0


class SourceError(Exception):
    """A source failed; the run continues without it."""


class QuotaExceededError(SourceError):
    """Free-tier quota is (nearly) used up; skip gracefully."""


@runtime_checkable
class JobSource(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def label(self) -> str:  # shown to users, e.g. "Adzuna"
        ...

    @property
    def credit_url(self) -> str | None:  # attribution link some sources' terms require
        ...

    async def search(self, queries: list[SearchQuery]) -> list[JobPosting]: ...


_GENERIC = {
    "senior",
    "junior",
    "jr",
    "sr",
    "intern",
    "internship",
    "fresher",
    "graduate",
    "remote",
    "india",
    "the",
    "and",
    "of",
    "for",
    "in",
    "at",
    "with",
    "entry",
    "level",
    "trainee",
    "associate",
    "lead",
    "staff",
    "i",
    "ii",
    "iii",
}
_ROLE_NOUNS = {
    "engineer",
    "developer",
    "programmer",
    "sde",
    "analyst",
    "scientist",
    "designer",
    "manager",
    "architect",
    "consultant",
    "specialist",
}


def query_terms(keywords: str) -> set[str]:
    return {t for t in norm_title(keywords).split() if t not in _GENERIC and len(t) > 1}


def title_matches(title: str, queries: list[SearchQuery]) -> bool:
    """Local keyword match for board sources that can't search server-side.

    A title matches a query when it shares a specific term (e.g. "backend", "python",
    "data") or is a close fuzzy match. A bare role noun ("engineer") alone isn't enough.
    """
    tokens = set(norm_title(title).split())
    for q in queries:
        terms = query_terms(q.keywords)
        specific = terms - _ROLE_NOUNS
        if specific & tokens:
            return True
        if not specific and terms & tokens:
            return True
        if fuzz.token_set_ratio(norm_title(q.keywords), norm_title(title)) >= 85:
            return True
    return False


def clean_company(name: str | None, fallback: str) -> str:
    name = (name or "").strip()
    return re.sub(r"\s+", " ", name) or fallback
