from __future__ import annotations

import asyncio
import logging

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteWebhook, GetMe, GetUpdates, SetMyCommands

from jobpilot.bot.runner import ConflictWatchdog, preflight_polling_check, run_bot, startup_call
from jobpilot.core.errors import ConfigError, TelegramConflictDetectedError
from jobpilot.core.lock import lock_is_free
from tests.fakes import error, ok

pytestmark = pytest.mark.integration

CONFLICT = error(409, "Conflict: terminated by other getUpdates request")


async def test_preflight_detects_another_poller(bot, telegram) -> None:
    telegram.handlers[GetUpdates] = lambda m: CONFLICT
    with pytest.raises(TelegramConflictDetectedError, match="already polling"):
        await preflight_polling_check(bot)


async def test_preflight_does_not_acknowledge_updates(bot, telegram) -> None:
    await preflight_polling_check(bot)
    [call] = telegram.of_type(GetUpdates)
    assert call.offset is None
    assert call.timeout == 0


async def test_startup_call_retries_network_errors() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TelegramNetworkError(method=GetMe(), message="offline")
        return "ok"

    assert await startup_call("t", flaky, initial_wait=0, max_wait=0) == "ok"
    assert attempts == 3


async def test_watchdog_stops_polling_after_repeated_conflicts() -> None:
    stopped = asyncio.Event()

    class FakeDp:
        async def stop_polling(self) -> None:
            stopped.set()

    watchdog = ConflictWatchdog(FakeDp(), threshold=3)  # type: ignore[arg-type]
    watchdog.install()
    try:
        log = logging.getLogger("aiogram.dispatcher")
        log.error("Failed to fetch updates - %s: %s", "TelegramNetworkError", "x")
        for _ in range(3):
            log.error("Failed to fetch updates - %s: %s", "TelegramConflictError", "409")
        await asyncio.wait_for(stopped.wait(), timeout=1)
    finally:
        watchdog.uninstall()
    assert watchdog.tripped


async def test_run_bot_rejected_token_is_a_config_error(settings, telegram) -> None:
    telegram.handlers[GetMe] = lambda m: error(401, "Unauthorized")
    with pytest.raises(ConfigError, match="rejected by Telegram"):
        await run_bot(settings, telegram_session=telegram)
    assert lock_is_free(settings.lock_path)


async def test_run_bot_exits_cleanly_when_another_instance_polls(settings, telegram) -> None:
    """Full startup: migrate, getMe, deleteWebhook, setMyCommands, poll, then 409 storm."""
    calls = {"n": 0}

    def get_updates(method: GetUpdates):
        calls["n"] += 1
        return ok([]) if calls["n"] == 1 else CONFLICT  # preflight OK, then conflicts

    telegram.handlers[GetUpdates] = get_updates
    with pytest.raises(TelegramConflictDetectedError):
        await asyncio.wait_for(run_bot(settings, telegram_session=telegram), timeout=20)

    assert telegram.of_type(DeleteWebhook)[0].drop_pending_updates is False
    [commands] = telegram.of_type(SetMyCommands)
    assert [c.command for c in commands.commands][:2] == ["start", "new"]
    assert len(commands.commands) == 13
    assert (settings.data_dir / "jobpilot.db").exists()
    assert lock_is_free(settings.lock_path)
