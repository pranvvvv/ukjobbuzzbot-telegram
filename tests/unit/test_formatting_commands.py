from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import Any

from jobpilot.bot.commands import COMMANDS, available_commands, lookup, telegram_commands
from jobpilot.bot.formatting import esc, help_text, status_text, utf16_len, welcome_text
from jobpilot.bot.keyboards import MenuAction, MenuCb, main_menu
from jobpilot.services.users import UserStatus


def _status(**kw: Any) -> UserStatus:
    base: dict[str, Any] = {
        "telegram_id": 1,
        "is_new": False,
        "has_profile": True,
        "profile_version": 2,
        "digest_enabled": True,
        "next_digest_at": datetime(2026, 11, 22, 3, 30, tzinfo=UTC),
        "digest_time": time(9, 0),
        "timezone": "Asia/Kolkata",
    }
    base.update(kw)
    return UserStatus(**base)


def test_command_specs_respect_telegram_limits() -> None:
    for c in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.name)
        assert 1 <= len(c.description) <= 256
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names))
    assert set(names) >= {"start", "new", "search", "deleteme", "help", "settime"}


def test_all_spec_commands_are_registered() -> None:
    expected = [
        "start",
        "new",
        "profile",
        "preferences",
        "search",
        "pause",
        "resume",
        "saved",
        "history",
        "stats",
        "settime",
        "deleteme",
        "help",
    ]
    assert [c.name for c in available_commands()] == expected
    assert [c.command for c in telegram_commands()] == expected
    assert lookup("SEARCH") is not None
    assert lookup("nope") is None


def test_esc_and_utf16_len() -> None:
    assert esc("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"
    assert utf16_len("🟢 87%") == 6  # the emoji counts as 2 UTF-16 units


def test_welcome_escapes_name_and_states_privacy() -> None:
    text = welcome_text("<script>")
    assert "&lt;script&gt;" in text
    assert "<script>" not in text
    assert "Privacy" in text
    assert "/deleteme" in text
    assert "PDF" in text
    assert "DOCX" in text


def test_status_with_profile_and_digest_on_shows_local_time() -> None:
    text = status_text(_status(), "Pranav")
    assert "version 2" in text
    assert "Sun 22 Nov, 09:00" in text


def test_status_paused_and_no_profile() -> None:
    assert "paused" in status_text(_status(digest_enabled=False, next_digest_at=None), None)
    assert "don't have your resume" in status_text(_status(has_profile=False), None)


def test_help_lists_commands() -> None:
    text = help_text()
    assert "/start" in text
    assert "/help" in text
    assert "/deleteme" in text


def test_main_menu_buttons_and_callback_sizes() -> None:
    assert main_menu(_status(has_profile=False)) is None
    markup = main_menu(_status())
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        "📄 New resume",
        "⚙️ Edit preferences",
        "🔎 Search now",
        "⏸ Pause digest",
    ]
    for b in buttons:
        assert b.callback_data is not None
        assert len(b.callback_data.encode()) <= 64
    paused = main_menu(_status(digest_enabled=False))
    assert paused is not None
    assert paused.inline_keyboard[-1][-1].text == "▶️ Resume digest"
    last = buttons[-1].callback_data
    assert last is not None
    assert MenuCb.unpack(last).action is MenuAction.PAUSE
