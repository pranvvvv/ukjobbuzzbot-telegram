"""User use-cases: registration, status, digest pause/resume.

The bot layer calls these; they know nothing about Telegram, so a web UI could reuse them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.config import Settings
from jobpilot.core.timeutil import next_local_occurrence
from jobpilot.db.models import User
from jobpilot.db.repo import ProfileRepo, UserRepo


@dataclass(frozen=True, slots=True)
class UserStatus:
    telegram_id: int
    is_new: bool
    has_profile: bool
    profile_version: int | None
    digest_enabled: bool
    next_digest_at: datetime | None
    digest_time: time
    timezone: str


class UserService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings

    async def register_or_get(self, telegram_id: int, name: str | None) -> UserStatus:
        async with self._sessions.begin() as session:
            users = UserRepo(session)
            user = await users.get(telegram_id)
            is_new = user is None
            if user is None:
                user = await users.create(
                    telegram_id,
                    name=name,
                    timezone=self._settings.default_timezone,
                    digest_time=self._settings.default_digest_time,
                    jobs_per_digest=self._settings.default_jobs_per_digest,
                )
            elif name and user.name != name:
                user.name = name
            return await self._status(session, telegram_id, is_new=is_new)

    async def status(self, telegram_id: int) -> UserStatus | None:
        async with self._sessions() as session:
            if await UserRepo(session).get(telegram_id) is None:
                return None
            return await self._status(session, telegram_id, is_new=False)

    async def set_digest_enabled(self, telegram_id: int, enabled: bool) -> UserStatus | None:
        async with self._sessions.begin() as session:
            users = UserRepo(session)
            user = await users.get(telegram_id)
            if user is None:
                return None
            await users.set_digest_enabled(user, enabled)
            return await self._status(session, telegram_id, is_new=False)

    async def apply_digest_settings(
        self,
        telegram_id: int,
        *,
        digest_time: time | None = None,
        jobs_per_digest: int | None = None,
        enable: bool | None = None,
        timezone: str | None = None,
    ) -> UserStatus | None:
        """Update digest settings and recompute the next run (UTC) from the new local time."""
        async with self._sessions.begin() as session:
            users = UserRepo(session)
            user = await users.get(telegram_id)
            if user is None:
                return None
            if digest_time is not None:
                user.digest_time = digest_time
            if jobs_per_digest is not None:
                user.jobs_per_digest = jobs_per_digest
            if timezone is not None:
                user.timezone = timezone
            await users.set_digest_enabled(user, user.digest_enabled if enable is None else enable)
            return await self._status(session, telegram_id, is_new=False)

    async def due_for_digest(self, now: datetime) -> list[int]:
        """Users whose digest time has passed. A laptop that was off for days still
        yields each user once: catch-up is coalesced by construction (ADR-004)."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(User.telegram_id).where(
                    User.digest_enabled.is_(True),
                    User.is_blocked.is_(False),
                    User.next_digest_at.is_not(None),
                    User.next_digest_at <= now,
                )
            )
            return list(rows.scalars())

    async def repair_schedules(self, now: datetime) -> int:
        """Enabled users with no next run (e.g. after a crash) get one computed."""
        async with self._sessions.begin() as session:
            users = (
                (
                    await session.execute(
                        select(User).where(
                            User.digest_enabled.is_(True), User.next_digest_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for user in users:
                user.next_digest_at = next_local_occurrence(user.digest_time, user.timezone, now)
            return len(users)

    async def digest_done(self, telegram_id: int, now: datetime) -> datetime | None:
        async with self._sessions.begin() as session:
            user = await session.get(User, telegram_id)
            if user is None:
                return None
            user.last_digest_at = now
            user.next_digest_at = (
                next_local_occurrence(user.digest_time, user.timezone, now)
                if user.digest_enabled
                else None
            )
            return user.next_digest_at

    async def retry_digest_at(self, telegram_id: int, when: datetime) -> None:
        async with self._sessions.begin() as session:
            user = await session.get(User, telegram_id)
            if user is not None and user.digest_enabled:
                user.next_digest_at = when

    async def mark_blocked(self, telegram_id: int) -> None:
        """The user blocked the bot: stop digests (spec 9)."""
        async with self._sessions.begin() as session:
            user = await session.get(User, telegram_id)
            if user is not None:
                user.is_blocked = True
                user.digest_enabled = False
                user.next_digest_at = None

    async def unblock(self, telegram_id: int) -> None:
        async with self._sessions.begin() as session:
            user = await session.get(User, telegram_id)
            if user is not None and user.is_blocked:
                user.is_blocked = False

    async def jobs_per_digest(self, telegram_id: int) -> int:
        async with self._sessions() as session:
            user = await session.get(User, telegram_id)
            return user.jobs_per_digest if user else 10

    @staticmethod
    async def _status(session: AsyncSession, telegram_id: int, *, is_new: bool) -> UserStatus:
        user = await UserRepo(session).get(telegram_id)
        if user is None:  # pragma: no cover - callers guarantee existence
            raise LookupError(telegram_id)
        profile = await ProfileRepo(session).active_for(telegram_id)
        return UserStatus(
            telegram_id=telegram_id,
            is_new=is_new,
            has_profile=profile is not None,
            profile_version=profile.version if profile else None,
            digest_enabled=user.digest_enabled,
            next_digest_at=user.next_digest_at,
            digest_time=user.digest_time,
            timezone=user.timezone,
        )
