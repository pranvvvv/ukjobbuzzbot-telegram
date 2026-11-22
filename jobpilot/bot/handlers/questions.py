"""Clarifying-question flow, search plan and /preferences (spec 4.4).

All progress lives in FSM data (SQL-backed), so a restart mid-flow resumes: old buttons
keep working because every callback re-reads the state instead of trusting the message.
"""

from __future__ import annotations

import contextlib
from datetime import time
from typing import Any, Protocol

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from jobpilot.bot.formatting import NO_PROFILE_YET, esc
from jobpilot.bot.questions_ui import (
    QAction,
    QCb,
    edit_menu,
    plan_keyboard,
    plan_text,
    question_by_index,
    render_question,
)
from jobpilot.bot.states import Flow
from jobpilot.core.config import Settings
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.preferences import (
    BY_ID,
    InputError,
    Preferences,
    PreferenceService,
    Question,
    apply_choice,
    apply_text,
    next_question,
    options_for,
    prefill,
    prev_question,
    selected_values,
    skip,
)
from jobpilot.services.profile import ProfileService
from jobpilot.services.users import UserService


class SearchLauncher(Protocol):
    async def search_now(self, bot: Bot, chat_id: int, user_id: int) -> None: ...


# ------------------------------------------------------------------ helpers
async def _show(
    bot: Bot, chat_id: int, state: FSMContext, text: str, markup: Any, *, edit_id: int | None
) -> None:
    if edit_id is not None:
        with contextlib.suppress(TelegramBadRequest):  # "message is not modified" is fine
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=edit_id, reply_markup=markup
            )
            return
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await state.update_data(msg_id=sent.message_id)


async def _ask(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    q: Question,
    prefs: Preferences,
    profile: CandidateProfile,
    *,
    edit_id: int | None,
    custom: bool = False,
) -> None:
    sel = selected_values(q, prefs)
    await state.set_state(Flow.answering)
    await state.update_data(q=q.id, sel=sel, custom=custom, prefs=prefs.model_dump(mode="json"))
    text, markup = render_question(q, prefs, profile, sel, awaiting_custom=custom)
    await _show(bot, chat_id, state, text, markup, edit_id=edit_id)


async def _show_plan(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    prefs: Preferences,
    sources: list[str],
    *,
    edit_id: int | None,
    saved: bool,
) -> None:
    await state.set_state(Flow.reviewing_plan)
    await state.update_data(prefs=prefs.model_dump(mode="json"), saved=saved, q=None)
    await _show(
        bot,
        chat_id,
        state,
        plan_text(prefs, sources, saved=saved),
        plan_keyboard(saved=saved),
        edit_id=edit_id,
    )


async def _context(
    state: FSMContext, profile_service: ProfileService
) -> tuple[dict[str, Any], Preferences, CandidateProfile] | None:
    data = await state.get_data()
    if "prefs" not in data or "profile_id" not in data:
        return None
    profile = await profile_service.get(data["profile_id"])
    if profile is None:
        return None
    return data, Preferences.model_validate(data["prefs"]), profile


async def _advance(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    q: Question,
    prefs: Preferences,
    profile: CandidateProfile,
    sources: list[str],
    *,
    edit_id: int | None,
) -> None:
    data = await state.get_data()
    nxt = None if data.get("editing") else next_question(q.id, prefs, profile)
    if nxt is None:
        await _show_plan(
            bot, chat_id, state, prefs, sources, edit_id=edit_id, saved=bool(data.get("saved"))
        )
    else:
        await _ask(bot, chat_id, state, nxt, prefs, profile, edit_id=edit_id)


# ------------------------------------------------------------------ entry points
async def start_questions(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    profile_id: int,
    profile: CandidateProfile,
    settings: Settings,
) -> None:
    prefs = prefill(
        profile,
        jobs_per_digest=settings.default_jobs_per_digest,
        digest_time=settings.default_digest_time.strftime("%H:%M"),
    )
    await state.update_data(profile_id=profile_id, editing=False, saved=False)
    first = next_question(None, prefs, profile)
    assert first is not None
    await bot.send_message(
        chat_id, "👍 Great. A few quick questions so I only send jobs you'd actually want."
    )
    await _ask(bot, chat_id, state, first, prefs, profile, edit_id=None)


async def show_preferences(
    bot: Bot,
    chat_id: int,
    user_id: int,
    state: FSMContext,
    profile_service: ProfileService,
    preference_service: PreferenceService,
    settings: Settings,
    source_names: list[str],
) -> None:
    active = await profile_service.active(user_id)
    if active is None:
        await state.set_state(Flow.waiting_resume)
        await bot.send_message(chat_id, NO_PROFILE_YET)
        return
    profile_id, _version, profile = active
    loaded = await preference_service.load(user_id, profile_id)
    if loaded is None:
        await start_questions(bot, chat_id, state, profile_id, profile, settings)
        return
    prefs, weights = loaded
    await state.update_data(profile_id=profile_id, editing=False)
    await _show_plan(bot, chat_id, state, prefs, source_names, edit_id=None, saved=True)
    if weights.notes:
        await bot.send_message(
            chat_id,
            "🧠 <b>What I've learned from your feedback</b>\n"
            + "\n".join(f"• {esc(n)}" for n in weights.notes[-5:]),
        )


async def cmd_preferences(
    message: Message,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    preference_service: PreferenceService,
    settings: Settings,
    source_names: list[str],
) -> None:
    assert message.from_user is not None
    await show_preferences(
        bot,
        message.chat.id,
        message.from_user.id,
        state,
        profile_service,
        preference_service,
        settings,
        source_names,
    )


# ------------------------------------------------------------------ callbacks
async def on_question_cb(
    query: CallbackQuery,
    callback_data: QCb,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    source_names: list[str],
) -> None:
    ctx = await _context(state, profile_service)
    if ctx is None or await state.get_state() != Flow.answering.state:
        await query.answer("This question has expired. Send /preferences.", show_alert=True)
        return
    data, prefs, profile = ctx
    q = BY_ID[data["q"]]
    assert query.message is not None
    chat_id, msg_id = query.message.chat.id, query.message.message_id
    options = options_for(q, prefs, profile)
    sel: list[str] = list(data.get("sel", []))

    match callback_data.a:
        case QAction.TOGGLE:
            if not 0 <= callback_data.v < len(options):
                await query.answer()
                return
            value = options[callback_data.v][0]
            sel = [v for v in sel if v != value] if value in sel else [*sel, value]
            if q.id == "company_size":  # "Any" is exclusive
                sel = ["any"] if value == "any" and "any" in sel else [v for v in sel if v != "any"]
            await state.update_data(sel=sel)
            text, markup = render_question(q, prefs, profile, sel)
            await query.answer()
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        case QAction.DONE:
            if q.id == "roles" and not sel:
                await query.answer("Pick at least one role (or add your own).", show_alert=True)
                return
            await query.answer()
            prefs = apply_choice(q, sel, prefs)
            await _advance(bot, chat_id, state, q, prefs, profile, source_names, edit_id=msg_id)
        case QAction.PICK:
            if not 0 <= callback_data.v < len(options):
                await query.answer()
                return
            await query.answer()
            prefs = apply_choice(q, [options[callback_data.v][0]], prefs)
            await _advance(bot, chat_id, state, q, prefs, profile, source_names, edit_id=msg_id)
        case QAction.SKIP | QAction.KEEP:
            await query.answer()
            if callback_data.a is QAction.SKIP:
                prefs = skip(q, prefs)
            await _advance(bot, chat_id, state, q, prefs, profile, source_names, edit_id=msg_id)
        case QAction.BACK:
            prev = prev_question(q.id, prefs, profile)
            if prev is None:
                await query.answer("This is the first question.")
                return
            await query.answer()
            await _ask(bot, chat_id, state, prev, prefs, profile, edit_id=msg_id)
        case QAction.CUSTOM:
            await query.answer()
            await state.update_data(custom=True)
            text, markup = render_question(q, prefs, profile, sel, awaiting_custom=True)
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        case _:
            await query.answer()


async def on_plan_cb(
    query: CallbackQuery,
    callback_data: QCb,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    preference_service: PreferenceService,
    user_service: UserService,
    delivery: SearchLauncher,
    source_names: list[str],
) -> None:
    ctx = await _context(state, profile_service)
    if ctx is None or await state.get_state() not in {
        Flow.reviewing_plan.state,
        Flow.answering.state,
    }:
        await query.answer("This plan has expired. Send /preferences.", show_alert=True)
        return
    data, prefs, profile = ctx
    assert query.message is not None
    chat_id, msg_id = query.message.chat.id, query.message.message_id
    match callback_data.a:
        case QAction.EDIT if callback_data.v < 0:
            await query.answer()
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_text(  # type: ignore[union-attr]
                    "✏️ What would you like to change?", reply_markup=edit_menu(prefs, profile)
                )
        case QAction.EDIT:
            await query.answer()
            await state.update_data(editing=True)
            q = question_by_index(callback_data.v)
            await _ask(bot, chat_id, state, q, prefs, profile, edit_id=msg_id)
        case QAction.PLAN:
            await query.answer()
            await _show_plan(
                bot,
                chat_id,
                state,
                prefs,
                source_names,
                edit_id=msg_id,
                saved=bool(data.get("saved")),
            )
        case QAction.START:
            await query.answer("Saved!")
            user_id = query.from_user.id
            await preference_service.save(user_id, data["profile_id"], prefs)
            hh, mm = (int(x) for x in prefs.digest_time.split(":"))
            await user_service.apply_digest_settings(
                user_id,
                digest_time=time(hh, mm),
                jobs_per_digest=prefs.jobs_per_digest,
                enable=True,
            )
            await state.clear()
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_text(  # type: ignore[union-attr]
                    plan_text(prefs, source_names, saved=True) + "\n\n✅ <b>Saved.</b>"
                )
            await delivery.search_now(bot, chat_id, user_id)
        case _:
            await query.answer()


async def on_answer_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    profile_service: ProfileService,
    source_names: list[str],
) -> None:
    ctx = await _context(state, profile_service)
    if ctx is None or message.text is None:
        await message.answer("Send /preferences to continue.")
        return
    data, prefs, profile = ctx
    q = BY_ID[data["q"]]
    if q.kind != "text" and not data.get("custom"):
        await message.answer("Please tap one of the buttons above. ☝️")
        return
    try:
        prefs = apply_text(q, message.text, prefs)
    except InputError as exc:
        await message.answer(esc(str(exc)))
        return
    if q.kind == "multi":  # custom roles added: show the question again with them ticked
        await _ask(bot, message.chat.id, state, q, prefs, profile, edit_id=None)
        return
    await _advance(bot, message.chat.id, state, q, prefs, profile, source_names, edit_id=None)


def build_router() -> Router:
    router = Router(name="questions")
    router.message.register(cmd_preferences, Command("preferences"))
    router.callback_query.register(
        on_plan_cb, QCb.filter(F.a.in_({QAction.EDIT, QAction.PLAN, QAction.START}))
    )
    router.callback_query.register(on_question_cb, QCb.filter())
    router.message.register(
        on_answer_text, StateFilter(Flow.answering), F.text, ~F.text.startswith("/")
    )
    return router
