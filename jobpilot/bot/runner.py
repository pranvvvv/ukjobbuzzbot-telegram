"""Process lifecycle for `python -m jobpilot run`.

Order matters:
1. file lock (no second instance on this machine),
2. migrations,
3. Telegram startup calls, retried while offline (autostart runs before Wi-Fi is up),
4. a 409-Conflict preflight (another machine or container polling this token),
5. polling, with a watchdog that stops cleanly if conflicts start mid-run.

aiogram's polling loop logs and retries *every* error, including 409 Conflict, so
steps 4 and 5 are how we turn "two bots fighting forever" into a clear exit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    wait_exponential,
    wait_random,
)

from jobpilot.bot.app import build_bot, build_dispatcher
from jobpilot.bot.commands import telegram_commands
from jobpilot.container import build_container
from jobpilot.core.config import Settings
from jobpilot.core.errors import ConfigError, TelegramConflictDetectedError
from jobpilot.core.lock import InstanceLock
from jobpilot.core.logging import get_logger
from jobpilot.db.engine import create_engine, create_session_factory
from jobpilot.db.migrate import upgrade_to_head_async
from jobpilot.scheduler.jobs import DigestScheduler

log = get_logger(__name__)

CONFLICT_MESSAGE = (
    "Another process is already polling this bot token (a second terminal, Docker, or "
    "another computer). Stop it, then start JobPilot again."
)


class ConflictWatchdog(logging.Handler):
    """Stops polling after repeated 409 Conflicts logged by aiogram's dispatcher."""

    def __init__(self, dp: Dispatcher, threshold: int = 3, window_seconds: float = 120) -> None:
        super().__init__(level=logging.ERROR)
        self._dp = dp
        self._threshold = threshold
        self._window = window_seconds
        self._hits: deque[float] = deque()
        self._loop = asyncio.get_running_loop()
        self.tripped = False

    def emit(self, record: logging.LogRecord) -> None:
        if "TelegramConflictError" not in record.getMessage() or self.tripped:
            return
        now = time.monotonic()
        self._hits.append(now)
        while self._hits and now - self._hits[0] > self._window:
            self._hits.popleft()
        if len(self._hits) >= self._threshold:
            self.tripped = True
            self._loop.create_task(self._dp.stop_polling())

    def install(self) -> None:
        logging.getLogger("aiogram.dispatcher").addHandler(self)

    def uninstall(self) -> None:
        logging.getLogger("aiogram.dispatcher").removeHandler(self)


async def startup_call[T](
    label: str,
    factory: Callable[[], Awaitable[T]],
    *,
    initial_wait: float = 2.0,
    max_wait: float = 120.0,
) -> T:
    """Retry a Telegram call through network/server errors (forever, capped backoff).

    Autostart at login often runs before Wi-Fi connects; waiting beats crashing.
    Auth and validation errors are not retried: they need a human.
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type((TelegramNetworkError, TelegramServerError)),
        wait=wait_exponential(multiplier=initial_wait, max=max_wait) + wait_random(0, initial_wait),
        reraise=True,
        before_sleep=lambda rs: log.warning(
            "startup.retrying",
            step=label,
            attempt=rs.attempt_number,
            error=repr(rs.outcome.exception()) if rs.outcome else None,
        ),
    ):
        with attempt:
            return await factory()
    raise AssertionError("unreachable")  # pragma: no cover


async def preflight_polling_check(bot: Bot) -> None:
    """Fail fast if another process is long-polling this token.

    `getUpdates` with timeout=0 and no offset does not acknowledge anything, so pending
    updates stay queued for our real polling loop.
    """
    try:
        await bot(GetUpdates(limit=1, timeout=0))
    except TelegramConflictError:
        raise TelegramConflictDetectedError(CONFLICT_MESSAGE) from None


async def run_bot(settings: Settings, *, telegram_session: BaseSession | None = None) -> None:
    """Run until stopped. `telegram_session` lets tests swap in a fake Telegram API."""
    settings.ensure_dirs()
    with InstanceLock(settings.lock_path):
        url = settings.resolved_database_url
        await upgrade_to_head_async(url)
        engine = create_engine(url)
        sessions = create_session_factory(engine)
        container = build_container(settings, sessions)
        bot = build_bot(settings, telegram_session)
        dp = build_dispatcher(container)
        watchdog = ConflictWatchdog(dp)
        scheduler = DigestScheduler(bot, container.users, container.delivery, container.source_http)
        try:
            try:
                me = await startup_call("get_me", bot.get_me)
            except TelegramUnauthorizedError:
                raise ConfigError(
                    ["TELEGRAM_BOT_TOKEN was rejected by Telegram (revoked or mistyped)"]
                ) from None
            await startup_call("delete_webhook", lambda: bot.delete_webhook(False))
            await startup_call("preflight", lambda: preflight_polling_check(bot))
            await startup_call("set_commands", lambda: bot.set_my_commands(telegram_commands()))
            log.info(
                "bot.started",
                username=me.username,
                llm=container.llm.provider,
                sources=container.source_names,
            )
            watchdog.install()
            scheduler.start()
            await dp.start_polling(
                bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False
            )
            if watchdog.tripped:
                raise TelegramConflictDetectedError(CONFLICT_MESSAGE)
        finally:
            scheduler.shutdown()
            watchdog.uninstall()
            await bot.session.close()
            await container.aclose()
            await engine.dispose()
            log.info("bot.stopped")
