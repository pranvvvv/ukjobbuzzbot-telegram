"""Job-card buttons, /search, /saved, /history, /pause, /resume, /settime, /stats."""

from __future__ import annotations

import contextlib
from datetime import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from jobpilot.bot.delivery import (
    REASONS,
    Delivery,
    JAction,
    JobCb,
    card_keyboard,
    reason_keyboard,
)
from jobpilot.bot.formatting import esc, hidden_card
from jobpilot.core.timeutil import format_local, utcnow
from jobpilot.db.models import MatchStatus
from jobpilot.services.feedback import DismissReason, FeedbackAction, FeedbackService
from jobpilot.services.matches import MatchService
from jobpilot.services.preferences import parse_time
from jobpilot.services.stats import StatsService
from jobpilot.services.users import UserService

PAGE = 10


class PageCb(CallbackData, prefix="pg"):
    kind: str  # saved | history
    offset: int


class TimeCb(CallbackData, prefix="tm"):
    hhmm: str  # "0730": aiogram reserves ":" as its field separator


# ------------------------------------------------------------------ card buttons
async def on_job_cb(
    query: CallbackQuery,
    callback_data: JobCb,
    feedback_service: FeedbackService,
    match_service: MatchService,
) -> None:
    user_id, mid = query.from_user.id, callback_data.m
    msg = query.message
    action = callback_data.a

    async def set_markup(markup: object) -> None:
        if msg is not None:
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_reply_markup(reply_markup=markup)  # type: ignore[union-attr, arg-type]

    if action is JAction.DISMISS:
        await query.answer("Why not this one?")
        await set_markup(reason_keyboard(mid))
        return
    if action is JAction.BACK:
        view = await match_service.get(mid, user_id)
        await query.answer()
        if view:
            await set_markup(card_keyboard(view))
        return
    if action in {JAction.EXCLUDE_YES, JAction.EXCLUDE_NO}:
        # The company comes from the match, never from callback data (names may contain ":").
        view = await match_service.get(mid, user_id)
        if action is JAction.EXCLUDE_YES and view is not None:
            await feedback_service.exclude_company(user_id, view.company)
            await query.answer(f"Excluded {view.company}.")
            if msg is not None:
                with contextlib.suppress(TelegramBadRequest):
                    await query.message.edit_text(  # type: ignore[union-attr]
                        f"🚫 Got it, I won't show jobs from <b>{esc(view.company)}</b> again."
                    )
        else:
            await query.answer("OK, kept.")
            if msg is not None:
                with contextlib.suppress(TelegramBadRequest):
                    await query.message.delete()  # type: ignore[union-attr]
        return

    mapping = {
        JAction.SAVE: FeedbackAction.SAVE,
        JAction.UNSAVE: FeedbackAction.UNSAVE,
        JAction.MORE: FeedbackAction.MORE,
        JAction.APPLIED: FeedbackAction.APPLIED,
        JAction.REASON: FeedbackAction.DISMISS,
    }
    reason = DismissReason(callback_data.r) if action is JAction.REASON else None
    effect = await feedback_service.record(user_id, mid, mapping[action], reason)
    if effect.view is None:
        await query.answer("This job is no longer available.", show_alert=True)
        return
    toast = {
        JAction.SAVE: "Saved 💾",
        JAction.UNSAVE: "Removed from saved",
        JAction.MORE: "Noted: more like this ➕",
        JAction.APPLIED: "Marked as applied ✅",
        JAction.REASON: "Hidden 🙅",
    }[action]
    await query.answer(effect.note or toast)
    if action is JAction.REASON:
        label = dict(REASONS).get(callback_data.r, "")
        if msg is not None:
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_text(hidden_card(effect.view, label.lower()))  # type: ignore[union-attr]
        if effect.ask_exclude_company and msg is not None:
            kb = InlineKeyboardBuilder()
            company = effect.ask_exclude_company[:40]
            kb.button(text="🚫 Yes, exclude", callback_data=JobCb(a=JAction.EXCLUDE_YES, m=mid))
            kb.button(text="No", callback_data=JobCb(a=JAction.EXCLUDE_NO, m=mid))
            kb.adjust(2)
            await query.message.answer(  # type: ignore[union-attr]
                f"Exclude <b>{esc(company)}</b> from all future results?",
                reply_markup=kb.as_markup(),
            )
        return
    await set_markup(card_keyboard(effect.view))


# ------------------------------------------------------------------ commands
async def cmd_search(message: Message, bot: Bot, delivery: Delivery) -> None:
    assert message.from_user is not None
    await delivery.search_now(bot, message.chat.id, message.from_user.id)


def _page_text(kind: str, views: list, total: int, offset: int) -> str:  # type: ignore[type-arg]
    title = "💾 <b>Saved jobs</b>" if kind == "saved" else "🗂 <b>Jobs I've sent you</b>"
    if total == 0:
        return (
            title
            + "\n\nNothing here yet."
            + (" Tap 💾 Save on a job card to keep it." if kind == "saved" else "")
        )
    icons = {
        MatchStatus.SAVED: "💾",
        MatchStatus.APPLIED: "✅",
        MatchStatus.DISMISSED: "🙅",
        MatchStatus.SENT: "•",
    }
    lines = [f"{title} ({offset + 1}–{offset + len(views)} of {total})", ""]
    for v in views:
        lines.append(
            f'{icons.get(v.status, "•")} <a href="{esc(v.apply_url)}">{esc(v.title[:70])}</a>'
            f" · {esc(v.company[:40])} ({v.score}%)"
        )
    return "\n".join(lines)


def _page_keyboard(kind: str, total: int, offset: int):  # type: ignore[no-untyped-def]
    kb = InlineKeyboardBuilder()
    if offset > 0:
        kb.button(text="⬅️ Newer", callback_data=PageCb(kind=kind, offset=max(0, offset - PAGE)))
    if offset + PAGE < total:
        kb.button(text="Older ➡️", callback_data=PageCb(kind=kind, offset=offset + PAGE))
    return kb.as_markup() if (offset > 0 or offset + PAGE < total) else None


STATUSES = {
    "saved": {MatchStatus.SAVED},
    "history": {MatchStatus.SENT, MatchStatus.SAVED, MatchStatus.APPLIED, MatchStatus.DISMISSED},
}


async def _show_page(
    target: Message,
    user_id: int,
    kind: str,
    offset: int,
    match_service: MatchService,
    *,
    edit: bool,
) -> None:
    views, total = await match_service.page(user_id, STATUSES[kind], offset=offset, limit=PAGE)
    text, markup = _page_text(kind, views, total, offset), _page_keyboard(kind, total, offset)
    if edit:
        with contextlib.suppress(TelegramBadRequest):
            await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def cmd_saved(message: Message, match_service: MatchService) -> None:
    assert message.from_user is not None
    await _show_page(message, message.from_user.id, "saved", 0, match_service, edit=False)


async def cmd_history(message: Message, match_service: MatchService) -> None:
    assert message.from_user is not None
    await _show_page(message, message.from_user.id, "history", 0, match_service, edit=False)


async def on_page(query: CallbackQuery, callback_data: PageCb, match_service: MatchService) -> None:
    await query.answer()
    if isinstance(query.message, Message) and callback_data.kind in STATUSES:
        await _show_page(
            query.message,
            query.from_user.id,
            callback_data.kind,
            max(0, callback_data.offset),
            match_service,
            edit=True,
        )


async def cmd_pause(message: Message, user_service: UserService) -> None:
    assert message.from_user is not None
    await user_service.set_digest_enabled(message.from_user.id, False)
    await message.answer("⏸ Daily digest paused. /resume turns it back on.")


async def cmd_resume(message: Message, user_service: UserService) -> None:
    assert message.from_user is not None
    status = await user_service.set_digest_enabled(message.from_user.id, True)
    if status is None or not status.has_profile:
        await message.answer("Set up your profile first: send /start.")
        return
    assert status.next_digest_at is not None
    await message.answer(
        f"▶️ Daily digest on. Next one: {esc(format_local(status.next_digest_at, status.timezone))}."
    )


async def _apply_time(user_id: int, hhmm: str, user_service: UserService) -> str:
    hh, mm = (int(x) for x in hhmm.split(":"))
    status = await user_service.apply_digest_settings(user_id, digest_time=time(hh, mm))
    if status is None:
        return "Send /start first."
    nxt = (
        f" Next digest: {esc(format_local(status.next_digest_at, status.timezone))}."
        if status.digest_enabled and status.next_digest_at
        else " (Digest is paused: /resume.)"
    )
    return f"🕒 Digest time set to {hhmm}.{nxt}"


async def cmd_settime(message: Message, command: CommandObject, user_service: UserService) -> None:
    assert message.from_user is not None
    if command.args:
        parsed = parse_time(command.args)
        if parsed is None:
            await message.answer("Please use a time like /settime 08:30 or /settime 7pm.")
            return
        await message.answer(await _apply_time(message.from_user.id, parsed, user_service))
        return
    kb = InlineKeyboardBuilder()
    for hhmm in ("07:00", "08:00", "09:00", "12:00", "18:00", "21:00"):
        kb.button(text=hhmm, callback_data=TimeCb(hhmm=hhmm.replace(":", "")))
    kb.adjust(3)
    await message.answer(
        "🕒 When should the daily digest arrive? (Or send e.g. /settime 07:45)",
        reply_markup=kb.as_markup(),
    )


async def on_time(query: CallbackQuery, callback_data: TimeCb, user_service: UserService) -> None:
    await query.answer()
    raw = callback_data.hhmm
    hhmm = f"{raw[:2]}:{raw[2:]}" if len(raw) == 4 and raw.isdigit() else "09:00"
    text = await _apply_time(query.from_user.id, hhmm, user_service)
    if isinstance(query.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await query.message.edit_text(text)


async def cmd_stats(
    message: Message, stats_service: StatsService, user_service: UserService
) -> None:
    assert message.from_user is not None
    st = await stats_service.for_user(message.from_user.id)
    status = await user_service.status(message.from_user.id)
    lines = ["📈 <b>JobPilot stats</b>", "", f"Runs in the last 7 days: {st.runs_7d}"]
    if st.last_run:
        c = st.last_run["counts"] or {}
        assert isinstance(c, dict)
        when = st.last_run["at"]
        tz = status.timezone if status else "Asia/Kolkata"
        lines.append(
            f"Last run ({st.last_run['type']}, {esc(format_local(when, tz)) if when else '?'}): "  # type: ignore[arg-type]
            f"{c.get('found', 0)} found → {c.get('unique', 0)} unique → {c.get('strong', 0)} strong"
        )
    lines += ["", "<b>Job sources</b> (calls today / this month)"]
    for name, today, month, per_day, per_month in st.sources:
        limit = []
        if per_day is not None:
            limit.append(f"{per_day}/day")
        if per_month is not None:
            limit.append(f"{per_month}/month")
        lines.append(
            f"• {name}: {today} / {month}" + (f" (limit {', '.join(limit)})" if limit else "")
        )
    lines += [
        "",
        f"🤖 AI cost (estimated): ${st.cost_today:.4f} today · ${st.cost_month:.4f} this month",
    ]
    if st.matches:
        m = st.matches
        delivered = sum(m.get(k, 0) for k in ("sent", "saved", "applied", "dismissed"))
        lines.append(
            f"🎯 Matches: {delivered} sent · {m.get('saved', 0)} saved · "
            f"{m.get('applied', 0)} applied · {m.get('dismissed', 0)} hidden"
        )
    if status and status.digest_enabled and status.next_digest_at:
        lines.append(f"📬 Next digest: {esc(format_local(status.next_digest_at, status.timezone))}")
    tz = status.timezone if status else "Asia/Kolkata"
    lines.append(f"\n<i>Generated {format_local(utcnow(), tz)}</i>")
    await message.answer("\n".join(lines))


def build_router() -> Router:
    router = Router(name="jobs")
    router.callback_query.register(on_job_cb, JobCb.filter())
    router.callback_query.register(on_page, PageCb.filter())
    router.callback_query.register(on_time, TimeCb.filter())
    router.message.register(cmd_search, Command("search"))
    router.message.register(cmd_saved, Command("saved"))
    router.message.register(cmd_history, Command("history"))
    router.message.register(cmd_pause, Command("pause"))
    router.message.register(cmd_resume, Command("resume"))
    router.message.register(cmd_settime, Command("settime"))
    router.message.register(cmd_stats, Command("stats"))
    return router
