"""Which sources run, with what limits. Add a source here and in its own module."""

from __future__ import annotations

from dataclasses import dataclass

from jobpilot.core.config import Settings
from jobpilot.sources.adzuna import AdzunaSource
from jobpilot.sources.base import JobSource
from jobpilot.sources.boards import AshbySource, GreenhouseSource, LeverSource, load_companies
from jobpilot.sources.http import SourceHttp, SourceLimits
from jobpilot.sources.jsearch import JSearchSource
from jobpilot.sources.remote_boards import RemoteOKSource, RemotiveSource


@dataclass(frozen=True, slots=True)
class SourceSet:
    sources: list[JobSource]
    remote_only: frozenset[str]  # sources that only make sense when remote work is wanted

    def names(self) -> list[str]:
        return [s.label for s in self.sources]

    def for_modes(self, work_modes: list[str]) -> list[JobSource]:
        wants_remote = "remote" in work_modes
        return [s for s in self.sources if wants_remote or s.name not in self.remote_only]


def build_sources(settings: Settings, http: SourceHttp) -> SourceSet:
    sources: list[JobSource] = []
    if settings.adzuna_app_id and settings.adzuna_app_key:
        # Adzuna free tier (3rd-party reports): 25/min, 250/day, 2,500/month; stay under it.
        http.register(
            "adzuna",
            SourceLimits(
                per_minute=20,
                per_day=settings.adzuna_daily_limit,
                per_month=settings.adzuna_monthly_limit,
            ),
        )
        sources.append(
            AdzunaSource(
                http,
                settings.adzuna_app_id.get_secret_value(),
                settings.adzuna_app_key.get_secret_value(),
                country=settings.adzuna_country,
                max_days_old=settings.max_job_age_days,
            )
        )
    companies = load_companies(settings.companies_file)
    for cls in (GreenhouseSource, LeverSource, AshbySource):
        http.register(cls.name, SourceLimits(per_minute=60, concurrency=3))
        board = cls(http, companies)
        if board.companies:
            sources.append(board)
    if settings.rapidapi_key:
        http.register(
            "jsearch",
            SourceLimits(per_minute=5, per_day=6, per_month=settings.jsearch_monthly_limit),
        )
        sources.append(JSearchSource(http, settings.rapidapi_key.get_secret_value()))
    remote_only: set[str] = set()
    if settings.enable_remote_boards:
        http.register("remotive", SourceLimits(per_minute=4, per_day=40))
        http.register("remoteok", SourceLimits(per_minute=2, per_day=10))
        sources += [RemotiveSource(http), RemoteOKSource(http)]
        remote_only = {"remotive", "remoteok"}
    return SourceSet(sources, frozenset(remote_only))
