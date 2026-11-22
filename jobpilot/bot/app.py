"""Bot and dispatcher factories (pure construction: no network, easy to test)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode

from jobpilot.bot.fsm_storage import SQLStorage
from jobpilot.bot.handlers import all_routers
from jobpilot.bot.middlewares import AllowlistMiddleware
from jobpilot.container import Container
from jobpilot.core.config import Settings


def build_bot(settings: Settings, session: BaseSession | None = None) -> Bot:
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,  # job cards shouldn't unfurl company pages
        ),
    )


def build_dispatcher(container: Container) -> Dispatcher:
    c = container
    dp = Dispatcher(storage=SQLStorage(c.sessions))
    dp.update.outer_middleware(AllowlistMiddleware(c.settings.allowed_user_ids))
    dp.workflow_data.update(
        settings=c.settings,
        user_service=c.users,
        profile_service=c.profiles,
        preference_service=c.prefs,
        match_service=c.matches,
        feedback_service=c.feedback,
        stats_service=c.stats,
        delivery=c.delivery,
        source_names=c.source_names,
    )
    for router in all_routers():
        dp.include_router(router)
    return dp
