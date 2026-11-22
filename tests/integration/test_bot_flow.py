"""End-to-end through the real aiogram dispatcher with a fake Telegram API."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import structlog
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from jobpilot.bot.formatting import UNKNOWN_INPUT
from jobpilot.bot.keyboards import MenuAction, MenuCb
from jobpilot.db.models import Profile, User
from tests import factories
from tests.conftest import OWNER_ID, STRANGER_ID
from tests.fakes import callback_update, message_update

pytestmark = pytest.mark.integration


async def _add_profile(sessions, user_id: int = OWNER_ID) -> None:
    async with sessions.begin() as s:
        s.add(
            Profile(
                user_id=user_id,
                version=1,
                raw_resume_text="synthetic",
                profile_json=factories.profile().model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )


async def test_stranger_is_ignored_silently_and_logged(dp, bot, telegram, sessions) -> None:
    with structlog.testing.capture_logs() as logs:
        await dp.feed_update(bot, message_update(STRANGER_ID, "/start"))
    assert telegram.requests == []  # no reply at all
    async with sessions() as s:
        assert await s.get(User, STRANGER_ID) is None
    assert any(e["event"] == "allowlist.rejected" and e["user_id"] == STRANGER_ID for e in logs)


async def test_stranger_callbacks_are_ignored_too(dp, bot, telegram) -> None:
    await dp.feed_update(bot, callback_update(STRANGER_ID, MenuCb(action=MenuAction.PAUSE).pack()))
    assert telegram.requests == []


async def test_first_start_welcomes_and_registers(dp, bot, telegram, sessions, settings) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    [text] = telegram.sent_texts()
    assert "I'm <b>JobPilot</b>" in text
    assert "Send your resume" in text
    async with sessions() as s:
        user = await s.get(User, OWNER_ID)
    assert user is not None
    assert user.name == "Pranav"
    assert user.timezone == settings.default_timezone
    assert user.digest_enabled is False


async def test_returning_user_without_profile_is_asked_for_resume(dp, bot, telegram) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    second = telegram.of_type(SendMessage)[-1]
    assert "don't have your resume yet" in second.text
    assert second.reply_markup is None


async def test_returning_user_with_profile_gets_status_and_menu(
    dp, bot, telegram, sessions
) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    await _add_profile(sessions)
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    reply = telegram.of_type(SendMessage)[-1]
    assert "Profile: on file (version 1)" in reply.text
    assert "paused" in reply.text
    labels = [b.text for row in reply.reply_markup.inline_keyboard for b in row]
    assert labels[:3] == ["📄 New resume", "⚙️ Edit preferences", "🔎 Search now"]
    assert labels[3] == "▶️ Resume digest"


async def test_resume_then_pause_digest_buttons(dp, bot, telegram, sessions) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    await _add_profile(sessions)

    await dp.feed_update(bot, callback_update(OWNER_ID, MenuCb(action=MenuAction.RESUME).pack()))
    async with sessions() as s:
        user = await s.get(User, OWNER_ID)
    assert user.digest_enabled is True
    assert user.next_digest_at is not None
    assert user.next_digest_at > datetime.now(UTC)
    edited = telegram.of_type(EditMessageText)[-1]
    assert "Daily digest: on, next" in edited.text
    assert telegram.of_type(AnswerCallbackQuery)[-1].text == "Digest resumed."

    await dp.feed_update(bot, callback_update(OWNER_ID, MenuCb(action=MenuAction.PAUSE).pack()))
    async with sessions() as s:
        user = await s.get(User, OWNER_ID)
    assert user.digest_enabled is False
    assert user.next_digest_at is None


async def test_search_button_before_setup_explains(dp, bot, telegram, sessions) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    await _add_profile(sessions)
    await dp.feed_update(
        bot, callback_update(OWNER_ID, MenuCb(action=MenuAction.SEARCH_NOW).pack())
    )
    edited = telegram.of_type(EditMessageText)[-1]
    assert "finish the quick questions" in edited.text


async def test_help_and_fallbacks(dp, bot, telegram) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/help"))
    await dp.feed_update(bot, message_update(OWNER_ID, "/whatever"))
    await dp.feed_update(bot, message_update(OWNER_ID, "hello"))
    help_reply, unknown, chatter = telegram.sent_texts()
    assert "/start" in help_reply
    assert "/deleteme" in help_reply
    assert unknown == UNKNOWN_INPUT
    assert chatter == UNKNOWN_INPUT


async def test_stale_unknown_callback(dp, bot, telegram) -> None:
    await dp.feed_update(bot, callback_update(OWNER_ID, "old:button"))
    assert "expired" in telegram.of_type(AnswerCallbackQuery)[-1].text
