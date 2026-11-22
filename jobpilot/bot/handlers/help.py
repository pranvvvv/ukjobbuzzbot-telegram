"""/help."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from jobpilot.bot.formatting import help_text


async def cmd_help(message: Message) -> None:
    await message.answer(help_text())


def build_router() -> Router:
    router = Router(name="help")
    router.message.register(cmd_help, Command("help"))
    return router
