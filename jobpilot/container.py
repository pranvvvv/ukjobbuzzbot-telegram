"""Composition root: builds every service once. Tests pass fakes for LLM, embedder, HTTP."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.bot.delivery import Delivery, Sender
from jobpilot.core.config import Settings
from jobpilot.llm.base import Embedder, LLMClient
from jobpilot.llm.embeddings import build_embedder
from jobpilot.llm.providers import build_llm
from jobpilot.matching.links import LinkChecker
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.cost import CostGuard
from jobpilot.services.feedback import FeedbackService
from jobpilot.services.matches import MatchService
from jobpilot.services.preferences import FeedbackWeights, Preferences, PreferenceService
from jobpilot.services.profile import ProfileService
from jobpilot.services.search import SearchDeps, SearchService
from jobpilot.services.stats import StatsService
from jobpilot.services.users import UserService
from jobpilot.sources.http import SourceHttp
from jobpilot.sources.registry import SourceSet, build_sources


@dataclass
class Container:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    llm: LLMClient
    embedder: Embedder
    http_client: httpx.AsyncClient
    source_http: SourceHttp
    sources: SourceSet
    users: UserService
    profiles: ProfileService
    prefs: PreferenceService
    matches: MatchService
    feedback: FeedbackService
    search: SearchService
    stats: StatsService
    delivery: Delivery

    @property
    def source_names(self) -> list[str]:
        return self.sources.names()

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.http_client.aclose()


def build_container(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Container:
    llm = llm or build_llm(settings)
    embedder = embedder or build_embedder(
        settings.use_local_embeddings, settings.embedding_model, settings.cache_dir / "models"
    )
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds, connect=10.0), follow_redirects=True
    )
    source_http = SourceHttp(client, sessions)
    sources = build_sources(settings, source_http)
    users = UserService(sessions, settings)
    profiles = ProfileService(sessions, llm)
    prefs = PreferenceService(sessions)
    matches = MatchService(sessions)

    async def load_context(
        user_id: int,
    ) -> tuple[int, int, CandidateProfile, Preferences, FeedbackWeights] | None:
        active = await profiles.active(user_id)
        if active is None:
            return None
        pid, version, profile = active
        loaded = await prefs.load(user_id, pid)
        if loaded is None:
            return None
        return pid, version, profile, loaded[0], loaded[1]

    search = SearchService(
        SearchDeps(
            sessions=sessions,
            llm=llm,
            embedder=embedder,
            sources=sources,
            links=LinkChecker(client, sessions),
            cost=CostGuard(
                sessions,
                llm.model,
                per_run_tokens=settings.llm_max_tokens_per_run,
                daily_budget_usd=settings.llm_daily_budget_usd,
            ),
            matches=matches,
            load_context=load_context,
            threshold=settings.match_threshold,
            basic_threshold=settings.basic_match_threshold,
            max_age_days=settings.max_job_age_days,
            top_k=settings.stage1_top_k,
            max_queries=settings.max_queries_per_run,
            labels={s.name: s.label for s in sources.sources},
        )
    )
    sender = Sender(users)
    delivery = Delivery(
        search,
        matches,
        users,
        sender,
        labels={s.name: s.label for s in sources.sources},
        credits={s.name: s.credit_url for s in sources.sources},
    )
    return Container(
        settings=settings,
        sessions=sessions,
        llm=llm,
        embedder=embedder,
        http_client=client,
        source_http=source_http,
        sources=sources,
        users=users,
        profiles=profiles,
        prefs=prefs,
        matches=matches,
        feedback=FeedbackService(sessions, matches, profiles, prefs),
        search=search,
        stats=StatsService(sessions, source_http, matches),
        delivery=delivery,
    )
