"""Data access. Handlers and services never write SQL directly; they go through here."""

from __future__ import annotations

from datetime import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.core.timeutil import next_local_occurrence, utcnow
from jobpilot.db.models import Profile, User


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, telegram_id: int) -> User | None:
        return await self._s.get(User, telegram_id)

    async def create(
        self,
        telegram_id: int,
        *,
        name: str | None,
        timezone: str,
        digest_time: time,
        jobs_per_digest: int,
    ) -> User:
        user = User(
            telegram_id=telegram_id,
            name=name,
            timezone=timezone,
            digest_time=digest_time,
            jobs_per_digest=jobs_per_digest,
            digest_enabled=False,  # turned on only after the first confirmed search plan
        )
        self._s.add(user)
        await self._s.flush()
        return user

    async def set_digest_enabled(self, user: User, enabled: bool) -> None:
        user.digest_enabled = enabled
        user.next_digest_at = (
            next_local_occurrence(user.digest_time, user.timezone, utcnow()) if enabled else None
        )
        await self._s.flush()


class ProfileRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def active_for(self, user_id: int) -> Profile | None:
        stmt = (
            select(Profile)
            .where(Profile.user_id == user_id, Profile.is_active.is_(True))
            .order_by(Profile.version.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def next_version(self, user_id: int) -> int:
        stmt = select(func.coalesce(func.max(Profile.version), 0)).where(Profile.user_id == user_id)
        current: int = (await self._s.execute(stmt)).scalar_one()
        return current + 1
