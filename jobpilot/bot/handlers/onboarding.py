"""Resume intake, profile confirmation/correction, /profile, /new, /deleteme (spec 4.2–4.4, 4.7)."""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import Any

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from jobpilot.bot.formatting import (
    FIX_PROMPT,
    NO_PROFILE_YET,
    PASTE_RECEIVED,
    READING_RESUME,
    SEND_RESUME,
    USE_NEW_FOR_RESUME,
    esc,
    profile_card,
)
from jobpilot.bot.handlers.questions import start_questions
from jobpilot.bot.states import INTAKE_STATES, Flow
from jobpilot.core.config import Settings
from jobpilot.core.logging import get_logger
from jobpilot.resume.extract import (
    ResumeError,
    check_size,
    extract_file,
    extract_pasted,
    kind_for,
)
from jobpilot.services.profile import NotAResumeError, ProfileService

log = get_logger(__name__)

PASTE_DEBOUNCE_SECONDS = 3.0
MIN_PASTE_CHARS = 200
_paste_tasks: dict[int, asyncio.Task[None]] = {}


class PAction(StrEnum):
    CONFIRM = "ok"
    FIX = "fix"
    DONE_PASTE = "done"
    NEW = "new"
    NEW_RESET = "newr"
    CANCEL = "no"
    DELETE = "del"


class ProfileCb(CallbackData, prefix="p"):
    a: PAction


def _confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Looks right", callback_data=ProfileCb(a=PAction.CONFIRM))
    kb.button(text="✏️ Fix something", callback_data=ProfileCb(a=PAction.FIX))
    kb.adjust(2)
    return kb.as_markup()


def _done_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Done, read it", callback_data=ProfileCb(a=PAction.DONE_PASTE))
    return kb.as_markup()


# ------------------------------------------------------------------ core processing
async def process_resume(
    bot: Bot,
    chat_id: int,
    user_id: int,
    text: str,
    state: FSMContext,
    profile_service: ProfileService,
) -> None:
    status = await bot.send_message(chat_id, READING_RESUME)
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            analysis = await profile_service.analyze(text)
            profile_id = await profile_service.save_draft(user_id, text, analysis.profile)
    except NotAResumeError as exc:
        await state.set_state(Flow.waiting_resume)
        await state.update_data(paste=[])
        await status.edit_text(
            f"🤔 That doesn't look like a resume ({esc(exc.reason)}).\n\n{SEND_RESUME}"
        )
        return
    except Exception:
        log.exception("resume.processing_failed")
        await state.set_state(Flow.waiting_resume)
        await state.update_data(paste=[])
        await status.edit_text("😕 Something went wrong reading that. Please try again.")
        return
    await state.set_state(Flow.confirming_profile)
    await state.update_data(draft_profile_id=profile_id, paste=[])
    await status.edit_text(profile_card(analysis.profile), reply_markup=_confirm_keyboard())


async def _flush_paste(
    bot: Bot, chat_id: int, user_id: int, state: FSMContext, profile_service: ProfileService
) -> None:
    _paste_tasks.pop(user_id, None)
    data = await state.get_data()
    text = "\n".join(data.get("paste", []))
    if len(text.strip()) < MIN_PASTE_CHARS:
        await state.set_state(Flow.waiting_resume)
        await state.update_data(paste=[])
        await bot.send_message(chat_id, "That looks too short to be a resume. " + SEND_RESUME)
        return
    try:
        extracted = extract_pasted(text)
    except ResumeError as exc:
        await bot.send_message(chat_id, esc(str(exc)))
        return
    await process_resume(bot, chat_id, user_id, extracted.text, state, profile_service)


def _schedule_flush(delay: float, *args: Any) -> None:
    user_id = args[2]
    if task := _paste_tasks.get(user_id):
        task.cancel()

    async def later() -> None:
        await asyncio.sleep(delay)
        await _flush_paste(*args)

    _paste_tasks[user_id] = asyncio.create_task(later())


# ------------------------------------------------------------------ handlers
async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    if file.file_path is None:
        raise ResumeError("Telegram didn't give me that file. Please send it again.")
    buffer = await bot.download_file(file.file_path)
    return buffer.read() if buffer else b""


async def on_document(
    message: Message, bot: Bot, state: FSMContext, profile_service: ProfileService
) -> None:
    assert message.document is not None
    assert message.from_user is not None
    current = await state.get_state()
    has_profile = await profile_service.active(message.from_user.id) is not None
    if has_profile and current not in {
        s.state for s in (*INTAKE_STATES, Flow.confirming_profile, Flow.fixing_profile)
    }:
        await message.answer(USE_NEW_FOR_RESUME)
        return
    doc = message.document
    try:
        kind_for(doc.file_name, doc.mime_type)
        check_size(doc.file_size)
        data = await _download(bot, doc.file_id)
        extracted = extract_file(doc.file_name, data, doc.mime_type)
    except ResumeError as exc:
        await state.set_state(Flow.waiting_resume)
        await message.answer(esc(str(exc)))
        return
    if extracted.truncated:
        await message.answer("That's a long one; I'll read the first 30,000 characters.")
    await process_resume(
        bot, message.chat.id, message.from_user.id, extracted.text, state, profile_service
    )


async def on_paste(
    message: Message,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    paste_debounce: float = PASTE_DEBOUNCE_SECONDS,
) -> None:
    assert message.from_user is not None
    assert message.text is not None
    data = await state.get_data()
    chunks = [*data.get("paste", []), message.text]
    first = await state.get_state() != Flow.collecting_paste.state
    await state.set_state(Flow.collecting_paste)
    await state.update_data(paste=chunks)
    if first:
        await message.answer(PASTE_RECEIVED, reply_markup=_done_keyboard())
    _schedule_flush(
        paste_debounce, bot, message.chat.id, message.from_user.id, state, profile_service
    )


async def on_unprompted_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    paste_debounce: float = PASTE_DEBOUNCE_SECONDS,
) -> None:
    """No state: a long message from someone without a profile is probably a pasted resume."""
    assert message.from_user is not None
    if len(message.text or "") < MIN_PASTE_CHARS or await profile_service.active(
        message.from_user.id
    ):
        raise SkipHandler
    await on_paste(message, bot, state, profile_service, paste_debounce)


async def on_done_paste(
    query: CallbackQuery, bot: Bot, state: FSMContext, profile_service: ProfileService
) -> None:
    await query.answer()
    if task := _paste_tasks.pop(query.from_user.id, None):
        task.cancel()
    if await state.get_state() != Flow.collecting_paste.state:
        return
    assert query.message is not None
    await _flush_paste(bot, query.message.chat.id, query.from_user.id, state, profile_service)


async def on_confirm(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    settings: Settings,
) -> None:
    data = await state.get_data()
    profile_id = data.get("draft_profile_id")
    if profile_id is None or await state.get_state() not in {
        Flow.confirming_profile.state,
        Flow.fixing_profile.state,
    }:
        await query.answer("This card is out of date. Send /start.", show_alert=True)
        return
    await query.answer("Saved!")
    await profile_service.activate(query.from_user.id, profile_id)
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    profile = await profile_service.get(profile_id)
    assert profile is not None
    assert query.message is not None
    await start_questions(bot, query.message.chat.id, state, profile_id, profile, settings)


async def on_fix(query: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != Flow.confirming_profile.state:
        await query.answer("This card is out of date. Send /start.", show_alert=True)
        return
    await query.answer()
    await state.set_state(Flow.fixing_profile)
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(FIX_PROMPT)


async def on_fix_text(
    message: Message, state: FSMContext, profile_service: ProfileService, bot: Bot
) -> None:
    data = await state.get_data()
    profile_id = data.get("draft_profile_id")
    if profile_id is None or not message.text:
        await message.answer("Send /start to begin again.")
        return
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        profile = await profile_service.correct(profile_id, message.text)
    await state.set_state(Flow.confirming_profile)
    await message.answer(profile_card(profile), reply_markup=_confirm_keyboard())


async def cmd_profile(message: Message, profile_service: ProfileService) -> None:
    assert message.from_user is not None
    active = await profile_service.active(message.from_user.id)
    if active is None:
        await message.answer(NO_PROFILE_YET)
        return
    _pid, version, profile = active
    await message.answer(
        profile_card(profile, draft=False)
        + f"\n\n<i>Version {version}. Use /new to replace it.</i>"
    )


async def cmd_new(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Yes, new resume", callback_data=ProfileCb(a=PAction.NEW))
    kb.button(text="📄 Yes + forget job history", callback_data=ProfileCb(a=PAction.NEW_RESET))
    kb.button(text="Cancel", callback_data=ProfileCb(a=PAction.CANCEL))
    kb.adjust(1)
    await state.set_state(Flow.confirming_new)
    await message.answer(
        "🔄 <b>Start fresh?</b>\nYour current profile is archived and the digest pauses until "
        "you finish setup.\n\nBy default I still remember jobs I've already sent you, so you "
        "won't see repeats. Choose <i>forget job history</i> to allow them again.",
        reply_markup=kb.as_markup(),
    )


async def on_new_choice(
    query: CallbackQuery,
    callback_data: ProfileCb,
    state: FSMContext,
    profile_service: ProfileService,
) -> None:
    await query.answer()
    if await state.get_state() != Flow.confirming_new.state:
        return
    reset = callback_data.a is PAction.NEW_RESET
    await profile_service.archive(query.from_user.id, reset_history=reset)
    await state.clear()
    await state.set_state(Flow.waiting_resume)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "🗂 Old profile archived"
            + (" and job history cleared." if reset else ".")
            + "\n\n"
            + SEND_RESUME
        )


async def cmd_deleteme(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Yes, delete everything", callback_data=ProfileCb(a=PAction.DELETE))
    kb.button(text="Cancel", callback_data=ProfileCb(a=PAction.CANCEL))
    kb.adjust(1)
    await state.set_state(Flow.confirming_delete)
    await message.answer(
        "⚠️ <b>Delete all your data?</b>\nThis permanently removes your resume text, "
        "profiles, preferences, job history and feedback from this computer. It can't be undone.",
        reply_markup=kb.as_markup(),
    )


async def on_delete(
    query: CallbackQuery, state: FSMContext, profile_service: ProfileService
) -> None:
    await query.answer()
    if await state.get_state() != Flow.confirming_delete.state:
        return
    await profile_service.delete_everything(query.from_user.id)
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "✅ Done. All your data has been deleted. Send /start if you ever want to begin again."
        )


async def on_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer("Cancelled.")
    if await state.get_state() in {Flow.confirming_new.state, Flow.confirming_delete.state}:
        await state.clear()
    if isinstance(query.message, Message):
        with contextlib.suppress(Exception):
            await query.message.edit_text("Cancelled. Nothing changed.")


async def on_resume_waiting_text(message: Message) -> None:
    await message.answer(SEND_RESUME)


def build_router() -> Router:
    router = Router(name="onboarding")
    router.message.register(on_document, F.document)
    router.message.register(cmd_profile, Command("profile"))
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_deleteme, Command("deleteme"))
    router.message.register(on_paste, StateFilter(*INTAKE_STATES), F.text, ~F.text.startswith("/"))
    router.message.register(
        on_fix_text, StateFilter(Flow.fixing_profile), F.text, ~F.text.startswith("/")
    )
    router.message.register(on_unprompted_text, StateFilter(None), F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_done_paste, ProfileCb.filter(F.a == PAction.DONE_PASTE))
    router.callback_query.register(on_confirm, ProfileCb.filter(F.a == PAction.CONFIRM))
    router.callback_query.register(on_fix, ProfileCb.filter(F.a == PAction.FIX))
    router.callback_query.register(
        on_new_choice, ProfileCb.filter(F.a.in_({PAction.NEW, PAction.NEW_RESET}))
    )
    router.callback_query.register(on_delete, ProfileCb.filter(F.a == PAction.DELETE))
    router.callback_query.register(on_cancel, ProfileCb.filter(F.a == PAction.CANCEL))
    return router
