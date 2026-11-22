"""Daily digests (spec 4.6, ADR-004).

The database is the source of truth: `users.next_digest_at` (UTC). A one-minute tick
delivers every due digest. If the laptop was asleep or off at digest time, the first
tick after startup finds the user due and sends ONE digest (coalesced, not one per
missed day), then schedules the next from "now".
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobpilot.bot.delivery import Delivery, SourcesDownError, UserBlockedError
from jobpilot.core.logging import bind_context, clear_context, get_logger
from jobpilot.core.timeutil import utcnow
from jobpilot.services.search import NotReadyError, SearchInProgressError
from jobpilot.services.users import UserService
from jobpilot.sources.http import SourceHttp

log = get_logger(__name__)
RETRY_AFTER = timedelta(minutes=30)
MAX_RETRIES = 3


class DigestScheduler:
    def __init__(
        self, bot: Bot, users: UserService, delivery: Delivery, http: SourceHttp | None = None
    ) -> None:
        self._bot = bot
        self._users = users
        self._delivery = delivery
        self._http = http
        self._retries: dict[int, int] = {}
        self._scheduler: AsyncIOScheduler | None = None

    async def tick(self) -> int:
        """Deliver every due digest. Returns how many users were processed."""
        now = utcnow()
        await self._users.repair_schedules(now)
        due = await self._users.due_for_digest(now)
        for user_id in due:
            bind_context(user_id=user_id, job="digest")
            try:
                await self._delivery.send_digest(self._bot, user_id)
            except SearchInProgressError:
                await self._users.retry_digest_at(user_id, now + timedelta(minutes=5))
                continue
            except UserBlockedError:
                continue  # Sender already disabled this user's digest
            except NotReadyError:
                log.info("digest.not_ready")
            except SourcesDownError as exc:
                n = self._retries.get(user_id, 0) + 1
                self._retries[user_id] = n
                if n <= MAX_RETRIES:
                    log.warning("digest.sources_down_retrying", attempt=n, error=str(exc))
                    await self._users.retry_digest_at(user_id, now + RETRY_AFTER)
                    if n == 1:
                        await self._delivery.sender.send(
                            self._bot,
                            user_id,
                            "😕 Job sources aren't reachable right now. I'll retry your digest "
                            "in 30 minutes.",
                        )
                    continue
                log.error("digest.gave_up_for_today", error=str(exc))  # noqa: TRY400
            except Exception:
                log.exception("digest.failed")
                await self._users.retry_digest_at(user_id, now + RETRY_AFTER)
                continue
            finally:
                clear_context()
            self._retries.pop(user_id, None)
            await self._users.digest_done(user_id, utcnow())
        return len(due)

    async def housekeeping(self) -> None:
        if self._http is not None:
            purged = await self._http.purge_expired()
            log.info("housekeeping.cache_purged", rows=purged)

    def start(self) -> AsyncIOScheduler:
        sched = AsyncIOScheduler(timezone="UTC")
        sched.add_job(
            self.tick,
            IntervalTrigger(minutes=1),
            id="digest-tick",
            next_run_time=utcnow() + timedelta(seconds=10),  # startup catch-up
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        sched.add_job(
            self.housekeeping,
            CronTrigger(hour=21, minute=30),
            id="housekeeping",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        sched.start()
        self._scheduler = sched
        return sched

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
