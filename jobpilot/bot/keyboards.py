"""Inline keyboards and their typed callback data (always well under Telegram's 64-byte limit)."""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from jobpilot.services.users import UserStatus


class MenuAction(StrEnum):
    NEW_RESUME = "new"
    EDIT_PREFS = "prefs"
    SEARCH_NOW = "search"
    PAUSE = "pause"
    RESUME = "resume"


class MenuCb(CallbackData, prefix="menu"):
    action: MenuAction


def main_menu(status: UserStatus) -> InlineKeyboardMarkup | None:
    if not status.has_profile:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 New resume", callback_data=MenuCb(action=MenuAction.NEW_RESUME))
    kb.button(text="⚙️ Edit preferences", callback_data=MenuCb(action=MenuAction.EDIT_PREFS))
    kb.button(text="🔎 Search now", callback_data=MenuCb(action=MenuAction.SEARCH_NOW))
    if status.digest_enabled:
        kb.button(text="⏸ Pause digest", callback_data=MenuCb(action=MenuAction.PAUSE))
    else:
        kb.button(text="▶️ Resume digest", callback_data=MenuCb(action=MenuAction.RESUME))
    kb.adjust(2)
    return kb.as_markup()
