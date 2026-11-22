"""/stats: runs, per-source quota use, AI tokens and estimated cost, match outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Run
from jobpilot.services.matches import MatchService
from jobpilot.sources.http import SourceHttp


@dataclass
class Stats:
    runs_7d: int = 0
    last_run: dict[str, object] | None = None
    cost_today: float = 0.0
    cost_month: float = 0.0
    tokens_note: str = ""
    sources: list[tuple[str, int, int, int | None, int | None]] = field(default_factory=list)
    matches: dict[str, int] = field(default_factory=dict)


class StatsService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], http: SourceHttp, matches: MatchService
    ) -> None:
        self._sessions = sessions
        self._http = http
        self._matches = matches

    async def for_user(self, user_id: int) -> Stats:
        now = utcnow()
        day_start = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
        month_start = day_start.replace(day=1)
        st = Stats()
        async with self._sessions() as session:
            st.runs_7d = (
                await session.execute(
                    select(func.count())
                    .select_from(Run)
                    .where(Run.user_id == user_id, Run.started_at >= now - timedelta(days=7))
                )
            ).scalar_one()
            last = (
                await session.execute(
                    select(Run)
                    .where(Run.user_id == user_id, Run.finished_at.is_not(None))
                    .order_by(Run.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if last is not None:
                st.last_run = {
                    "type": last.type.value,
                    "at": last.started_at,
                    "counts": last.counts_json,
                    "errors": last.errors_json,
                }
            for since, attr in ((day_start, "cost_today"), (month_start, "cost_month")):
                value = (
                    await session.execute(
                        select(func.coalesce(func.sum(Run.est_cost_usd), 0.0)).where(
                            Run.started_at >= since
                        )
                    )
                ).scalar_one()
                setattr(st, attr, float(value))
        for name, limits in sorted(self._http.registered().items()):
            today, month = await self._http.usage(name)
            st.sources.append((name, today, month, limits.per_day, limits.per_month))
        st.matches = await self._matches.counts(user_id)
        return st
