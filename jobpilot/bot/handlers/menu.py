"""Main-menu button callbacks (from /start)."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from jobpilot.bot.delivery import Delivery
from jobpilot.bot.formatting import status_text
from jobpilot.bot.handlers.onboarding import cmd_new
from jobpilot.bot.handlers.questions import show_preferences
from jobpilot.bot.keyboards import MenuAction, MenuCb, main_menu
from jobpilot.core.config import Settings
from jobpilot.services.preferences import PreferenceService
from jobpilot.services.profile import ProfileService
from jobpilot.services.users import UserService


async def toggle_digest(
    query: CallbackQuery, callback_data: MenuCb, user_service: UserService
) -> None:
    enabled = callback_data.action is MenuAction.RESUME
    status = await user_service.set_digest_enabled(query.from_user.id, enabled)
    if status is None:
        await query.answer("Send /start first.", show_alert=True)
        return
    await query.answer("Digest resumed." if enabled else "Digest paused.")
    if isinstance(query.message, Message):
        await query.message.edit_text(
            status_text(status, query.from_user.first_name), reply_markup=main_menu(status)
        )


async def on_new(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await cmd_new(query.message, state)


async def on_prefs(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    preference_service: PreferenceService,
    settings: Settings,
    source_names: list[str],
) -> None:
    await query.answer()
    assert query.message is not None
    await show_preferences(
        bot,
        query.message.chat.id,
        query.from_user.id,
        state,
        profile_service,
        preference_service,
        settings,
        source_names,
    )


async def on_search(query: CallbackQuery, bot: Bot, delivery: Delivery) -> None:
    await query.answer("Searching…")
    assert query.message is not None
    await delivery.search_now(bot, query.message.chat.id, query.from_user.id)


def build_router() -> Router:
    router = Router(name="menu")
    router.callback_query.register(
        toggle_digest, MenuCb.filter(F.action.in_({MenuAction.PAUSE, MenuAction.RESUME}))
    )
    router.callback_query.register(on_new, MenuCb.filter(F.action == MenuAction.NEW_RESUME))
    router.callback_query.register(on_prefs, MenuCb.filter(F.action == MenuAction.EDIT_PREFS))
    router.callback_query.register(on_search, MenuCb.filter(F.action == MenuAction.SEARCH_NOW))
    return router
