"""Bot middlewares."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User

from jobpilot.core.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AllowlistMiddleware(BaseMiddleware):
    """Drop every update not from a whitelisted Telegram user (spec rule 7).

    Registered as an *outer* middleware on `dp.update`, so it runs before any filter,
    FSM lookup or handler, for every update type. Rejections are silent to the sender
    and logged here (the log line also tells the owner their own ID during setup).
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id in self._allowed:
            return await handler(event, data)
        log.info(
            "allowlist.rejected",
            user_id=user.id if user else None,
            update_type=event.event_type if isinstance(event, Update) else type(event).__name__,
        )
        return None
