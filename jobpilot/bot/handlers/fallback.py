"""Catch-alls. Registered last, so they only see what no other router handled."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from jobpilot.bot.commands import lookup
from jobpilot.bot.formatting import NOT_YET_AVAILABLE, UNKNOWN_INPUT


async def unknown_or_future_command(message: Message) -> None:
    name = (message.text or "/").split()[0][1:].split("@")[0]
    spec = lookup(name)
    await message.answer(NOT_YET_AVAILABLE if spec and not spec.available else UNKNOWN_INPUT)


async def anything_else(message: Message) -> None:
    await message.answer(UNKNOWN_INPUT)


async def stale_button(query: CallbackQuery) -> None:
    await query.answer("This button has expired. Send /start for a fresh menu.")


def build_router() -> Router:
    router = Router(name="fallback")
    router.message.register(unknown_or_future_command, F.text.startswith("/"))
    router.message.register(anything_else)
    router.callback_query.register(stale_button)
    return router
