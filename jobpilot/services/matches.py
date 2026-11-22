"""Matches: card views, statuses (sent/saved/dismissed/applied), history pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Job, Match, MatchStatus


@dataclass(slots=True)
class MatchView:
    match_id: int
    job_id: int
    title: str
    company: str
    location: str | None
    remote_type: str | None
    employment_type: str | None
    salary_min: float | None
    salary_max: float | None
    currency: str | None
    salary_period: str | None
    salary_is_estimated: bool
    apply_url: str
    source: str
    posted_at: datetime | None
    score: int
    is_basic: bool
    status: MatchStatus
    why_fit: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    pitch: str | None = None


def to_view(m: Match, j: Job) -> MatchView:
    return MatchView(
        match_id=m.id,
        job_id=j.id,
        title=j.title,
        company=j.company,
        location=j.location,
        remote_type=j.remote_type,
        employment_type=j.employment_type,
        salary_min=j.salary_min,
        salary_max=j.salary_max,
        currency=j.currency,
        salary_period=j.salary_period,
        salary_is_estimated=j.salary_is_estimated,
        apply_url=j.apply_url,
        source=j.source,
        posted_at=j.posted_at,
        score=round(m.score),
        is_basic=m.is_basic,
        status=m.status,
        why_fit=list(m.reasons_json or []),
        missing_skills=list(m.missing_skills_json or []),
        red_flags=list(m.red_flags_json or []),
        pitch=m.pitch,
    )


class MatchService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, match_id: int, user_id: int) -> MatchView | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Match, Job)
                    .join(Job, Job.id == Match.job_id)
                    .where(Match.id == match_id, Match.user_id == user_id)
                )
            ).first()
            return to_view(*row) if row else None

    async def pending(
        self,
        user_id: int,
        *,
        threshold: int,
        limit: int,
        max_age_days: int,
        basic_threshold: int | None = None,
    ) -> list[MatchView]:
        """Strong, never-sent matches, best first (includes leftovers from earlier runs).

        AI-scored and basic (no-AI) scores sit on different scales, so each has its own bar.
        """
        cutoff = utcnow() - timedelta(days=max_age_days)
        basic_bar = threshold if basic_threshold is None else basic_threshold
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Match, Job)
                    .join(Job, Job.id == Match.job_id)
                    .where(
                        Match.user_id == user_id,
                        Match.status == MatchStatus.NEW,
                        ((Match.is_basic.is_(False)) & (Match.score >= threshold))
                        | ((Match.is_basic.is_(True)) & (Match.score >= basic_bar)),
                        (Job.posted_at.is_(None)) | (Job.posted_at >= cutoff),
                    )
                    .order_by(Match.score.desc(), Job.posted_at.desc())
                    .limit(limit)
                )
            ).all()
            return [to_view(m, j) for m, j in rows]

    async def set_status(
        self, match_id: int, user_id: int, status: MatchStatus
    ) -> MatchView | None:
        async with self._sessions.begin() as session:
            match = await session.get(Match, match_id)
            if match is None or match.user_id != user_id:
                return None
            match.status = status
            if status is MatchStatus.SENT and match.sent_at is None:
                match.sent_at = utcnow()
            job = await session.get(Job, match.job_id)
            assert job is not None
            return to_view(match, job)

    async def mark_sent(self, match_id: int, user_id: int) -> None:
        async with self._sessions.begin() as session:
            match = await session.get(Match, match_id)
            if match and match.user_id == user_id and match.status is MatchStatus.NEW:
                match.status = MatchStatus.SENT
                match.sent_at = utcnow()

    async def page(
        self, user_id: int, statuses: set[MatchStatus], *, offset: int, limit: int
    ) -> tuple[list[MatchView], int]:
        async with self._sessions() as session:
            base = (
                select(Match, Job)
                .join(Job, Job.id == Match.job_id)
                .where(Match.user_id == user_id, Match.status.in_(statuses))
            )
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
            rows = (
                await session.execute(
                    base.order_by(Match.sent_at.desc().nullslast(), Match.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return [to_view(m, j) for m, j in rows], int(total)

    async def counts(self, user_id: int) -> dict[str, int]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Match.status, func.count())
                    .where(Match.user_id == user_id)
                    .group_by(Match.status)
                )
            ).all()
            return {status.value: int(n) for status, n in rows}
