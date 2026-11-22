"""Sending results to Telegram (spec 4.5, 4.6, 9).

`Sender` is the global send queue: ≤1 message/second per chat and ≤25/second overall
(Telegram FAQ: avoid >1 msg/s per chat, ~30/s bulk), sleeps through `RetryAfter`
flood-waits, and disables digests if the user blocked the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from enum import StrEnum
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiolimiter import AsyncLimiter

from jobpilot.bot.formatting import esc, job_card
from jobpilot.core.logging import get_logger
from jobpilot.db.models import MatchStatus, RunType
from jobpilot.services.matches import MatchService, MatchView
from jobpilot.services.search import (
    NotReadyError,
    RunOutcome,
    SearchInProgressError,
    SearchService,
    top_missing_skills,
)
from jobpilot.services.users import UserService

log = get_logger(__name__)


class UserBlockedError(Exception):
    pass


class SourcesDownError(Exception):
    """Every source failed; the scheduler retries later."""


class JAction(StrEnum):
    SAVE = "s"
    UNSAVE = "u"
    DISMISS = "x"  # opens the reason picker
    REASON = "r"
    MORE = "m"
    APPLIED = "a"
    BACK = "b"
    EXCLUDE_YES = "ey"
    EXCLUDE_NO = "en"


class JobCb(CallbackData, prefix="j"):
    a: JAction
    m: int  # match id
    r: str = ""


REASONS = [
    ("role", "Wrong role"),
    ("location", "Location"),
    ("salary", "Salary"),
    ("company", "Company"),
    ("seniority", "Seniority"),
    ("other", "Other"),
]


def card_keyboard(view: MatchView) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Apply", url=view.apply_url)
    if view.status is MatchStatus.SAVED:
        kb.button(text="💾 Saved ✓", callback_data=JobCb(a=JAction.UNSAVE, m=view.match_id))
    else:
        kb.button(text="💾 Save", callback_data=JobCb(a=JAction.SAVE, m=view.match_id))
    kb.button(text="🙅 Not interested", callback_data=JobCb(a=JAction.DISMISS, m=view.match_id))
    kb.button(text="➕ More like this", callback_data=JobCb(a=JAction.MORE, m=view.match_id))
    applied = view.status is MatchStatus.APPLIED
    kb.button(
        text="✅ Applied ✓" if applied else "✅ Applied",
        callback_data=JobCb(a=JAction.APPLIED, m=view.match_id),
    )
    kb.adjust(1, 2, 2)
    return kb.as_markup()


def reason_keyboard(match_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, label in REASONS:
        kb.button(text=label, callback_data=JobCb(a=JAction.REASON, m=match_id, r=code))
    kb.button(text="⬅️ Back", callback_data=JobCb(a=JAction.BACK, m=match_id))
    kb.adjust(3, 3, 1)
    return kb.as_markup()


class Sender:
    def __init__(
        self, users: UserService, *, per_chat_interval: float = 1.05, global_rate: float = 25
    ) -> None:
        self._users = users
        self._interval = per_chat_interval
        self._global = AsyncLimiter(global_rate, 1)
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last: dict[int, float] = {}

    async def _call(self, chat: int, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        lock = self._chat_locks.setdefault(chat, asyncio.Lock())
        async with lock:
            wait = self._interval - (time.monotonic() - self._last.get(chat, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            for attempt in range(4):
                async with self._global:
                    try:
                        result = await fn(*args, **kwargs)
                    except TelegramRetryAfter as exc:
                        log.warning("telegram.flood_wait", seconds=exc.retry_after, attempt=attempt)
                        await asyncio.sleep(exc.retry_after + 0.5)
                    except TelegramForbiddenError:
                        log.warning("telegram.user_blocked_bot", chat_id=chat)
                        await self._users.mark_blocked(chat)
                        raise UserBlockedError from None
                    else:
                        self._last[chat] = time.monotonic()
                        return result
            raise RuntimeError("Telegram kept rate-limiting; giving up on this message")

    async def send(self, bot: Bot, chat_id: int, text: str, **kwargs: Any) -> Message:
        result: Message = await self._call(chat_id, bot.send_message, chat_id, text, **kwargs)
        return result

    async def edit(self, bot: Bot, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        with contextlib.suppress(TelegramBadRequest):  # "message is not modified"
            await self._call(
                chat_id,
                bot.edit_message_text,
                text,
                chat_id=chat_id,
                message_id=message_id,
                **kwargs,
            )


def summary_text(
    out: RunOutcome,
    sent: list[MatchView],
    labels: dict[str, str],
    *,
    header: str = "📊 <b>Search summary</b>",
) -> str:
    c = out.counts
    lines = [
        header,
        f"Checked {c.get('found', 0)} jobs → {c.get('unique', 0)} unique → "
        f"{c.get('strong', 0)} strong matches (score ≥ threshold).",
    ]
    if out.by_source:
        parts = [
            f"{labels.get(k, k)} {v}"
            for k, v in sorted(out.by_source.items(), key=lambda kv: -kv[1])
        ]
        lines.append("By source: " + ", ".join(parts))
    if sent:
        avg = round(sum(v.score for v in sent) / len(sent))
        lines.append(f"Average match score: {avg}%")
        if gaps := top_missing_skills(sent):
            lines.append(f"📚 Skills that keep coming up: <b>{esc(', '.join(gaps))}</b>")
    if out.basic_only and sent:
        lines.append("<i>AI scoring was unavailable, so these are basic matches.</i>")
    problems = [labels.get(k, k) for k in out.errors]
    if problems:
        lines.append(f"⚠️ Couldn't reach: {esc(', '.join(problems))} (results are partial).")
    if out.skipped:
        skipped = ", ".join(labels.get(k, k) for k in out.skipped)
        lines.append(f"⏸ Skipped to protect free quota: {esc(skipped)}")
    return "\n".join(lines)


class Delivery:
    def __init__(
        self,
        search: SearchService,
        matches: MatchService,
        users: UserService,
        sender: Sender,
        labels: dict[str, str],
        credits: dict[str, str | None],
    ) -> None:
        self._search = search
        self._matches = matches
        self._users = users
        self.sender = sender
        self._labels = labels
        self._credits = credits

    async def send_cards(self, bot: Bot, chat_id: int, user_id: int, views: list[MatchView]) -> int:
        sent = 0
        for view in views:
            text = job_card(
                view, self._labels.get(view.source, view.source), self._credits.get(view.source)
            )
            try:
                await self.sender.send(bot, chat_id, text, reply_markup=card_keyboard(view))
            except UserBlockedError:
                raise
            except TelegramBadRequest as exc:  # e.g. an apply URL Telegram rejects
                log.warning("card.rejected", match_id=view.match_id, error=str(exc))
                continue
            await self._matches.mark_sent(view.match_id, user_id)
            sent += 1
        return sent

    def _progress(self, bot: Bot, chat_id: int, message_id: int) -> Any:
        last_t = 0.0
        last_text = ""

        async def update(text: str) -> None:
            nonlocal last_t, last_text
            if text == last_text or time.monotonic() - last_t < 1.5:
                return
            last_t, last_text = time.monotonic(), text
            await self.sender.edit(bot, chat_id, message_id, text)

        return update

    async def search_now(self, bot: Bot, chat_id: int, user_id: int) -> None:
        if self._search.is_running(user_id):
            await self.sender.send(bot, chat_id, "⏳ A search is already running. Hang on…")
            return
        status = await self.sender.send(bot, chat_id, "🔎 Starting your search…")
        limit = await self._users.jobs_per_digest(user_id)
        try:
            out = await self._search.run(
                user_id,
                RunType.SEARCH,
                limit=limit,
                progress=self._progress(bot, chat_id, status.message_id),
            )
        except SearchInProgressError:
            await self.sender.edit(
                bot, chat_id, status.message_id, "⏳ A search is already running. Hang on…"
            )
            return
        except NotReadyError:
            await self.sender.edit(
                bot,
                chat_id,
                status.message_id,
                "First send me your resume and finish the quick questions. /start",
            )
            return
        c = out.counts
        await self.sender.edit(
            bot,
            chat_id,
            status.message_id,
            f"✅ Searched {out.sources_tried} sources · {c.get('found', 0)} found → "
            f"{c.get('unique', 0)} after dedupe → {c.get('strong', 0)} strong matches",
        )
        if out.all_sources_failed:
            await self.sender.send(
                bot,
                chat_id,
                "😕 I couldn't reach any job source just now (network or the sites are down). "
                "I'll try again automatically with your next digest.",
            )
            return
        sent = await self.send_cards(bot, chat_id, user_id, out.to_send)
        if sent == 0:
            await self.sender.send(
                bot,
                chat_id,
                f"No strong matches right now ({c.get('found', 0)} jobs checked). "
                "I'll keep looking and send new ones in your daily digest.",
            )
        await self.sender.send(bot, chat_id, summary_text(out, out.to_send[:sent], self._labels))

    async def send_digest(self, bot: Bot, user_id: int) -> int:
        """Run and deliver one digest. Raises SourcesDownError / UserBlockedError."""
        limit = await self._users.jobs_per_digest(user_id)
        out = await self._search.run(user_id, RunType.DIGEST, limit=limit)
        if out.all_sources_failed:
            raise SourcesDownError(", ".join(out.errors))
        if not out.to_send:
            await self.sender.send(
                bot,
                user_id,
                f"☀️ No strong new matches today; {out.counts.get('found', 0)} jobs checked.",
            )
            return 0
        await self.sender.send(
            bot,
            user_id,
            f"☀️ <b>Your daily JobPilot digest</b>: {len(out.to_send)} new match"
            + ("es" if len(out.to_send) != 1 else ""),
        )
        sent = await self.send_cards(bot, user_id, user_id, out.to_send)
        await self.sender.send(
            bot,
            user_id,
            summary_text(out, out.to_send[:sent], self._labels, header="📊 <b>Digest summary</b>"),
        )
        return sent
