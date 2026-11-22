"""/start: welcome for new users, status + main menu for returning users (spec 4.1)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from jobpilot.bot.formatting import status_text, welcome_text
from jobpilot.bot.keyboards import main_menu
from jobpilot.bot.states import Flow
from jobpilot.services.users import UserService


async def cmd_start(message: Message, user_service: UserService, state: FSMContext) -> None:
    if message.from_user is None:  # pragma: no cover - allowlist already requires a user
        return
    first_name = message.from_user.first_name
    await user_service.unblock(message.from_user.id)
    status = await user_service.register_or_get(message.from_user.id, first_name)
    if status.is_new:
        await state.set_state(Flow.waiting_resume)
        await message.answer(welcome_text(first_name))
        return
    if not status.has_profile:
        await state.set_state(Flow.waiting_resume)
    elif await state.get_state() in {Flow.confirming_new.state, Flow.confirming_delete.state}:
        await state.clear()
    await message.answer(status_text(status, first_name), reply_markup=main_menu(status))


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(cmd_start, CommandStart())
    return router
