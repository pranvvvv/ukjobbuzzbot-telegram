"""Rendering for clarifying questions and the search-plan card (spec 4.4)."""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from jobpilot.bot.formatting import LEVEL_LABEL, esc
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.preferences import (
    BY_ID,
    QUESTIONS,
    Preferences,
    Question,
    applicable,
    options_for,
    position,
)


class QAction(StrEnum):
    TOGGLE = "t"
    DONE = "d"
    PICK = "p"
    BACK = "b"
    SKIP = "s"
    CUSTOM = "c"
    KEEP = "k"
    EDIT = "e"  # from the plan card: jump to a question
    START = "go"
    PLAN = "pl"  # back to the plan


class QCb(CallbackData, prefix="q"):
    a: QAction
    v: int = 0  # option index (labels can be longer than Telegram's 64-byte limit)


def _current_text_value(q: Question, prefs: Preferences) -> str | None:
    if q.id == "locations" and prefs.locations:
        return ", ".join(prefs.locations)
    if q.id == "salary" and prefs.min_salary:
        return prefs.min_salary.raw
    if q.id == "exclude" and prefs.excluded_companies:
        return ", ".join(prefs.excluded_companies)
    if q.id == "keywords" and (prefs.must_have_keywords or prefs.excluded_keywords):
        parts = [*prefs.must_have_keywords, *(f"no {k}" for k in prefs.excluded_keywords)]
        return ", ".join(parts)
    return None


def render_question(
    q: Question,
    prefs: Preferences,
    profile: CandidateProfile,
    selected: list[str],
    *,
    awaiting_custom: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    idx, total = position(q.id, prefs, profile)
    lines = [f"<b>Question {idx} of {total}</b>", "", f"<b>{esc(q.text)}</b>"]
    if q.hint:
        lines.append(f"<i>{esc(q.hint)}</i>")
    current = _current_text_value(q, prefs) if q.kind == "text" else None
    if current:
        lines.append(f"Current: {esc(current)}")
    if awaiting_custom:
        lines.append(
            "✍️ Type it now"
            + (" (comma-separated)." if q.id == "roles" else " (e.g. 08:30 or 7pm).")
        )

    kb = InlineKeyboardBuilder()
    options = options_for(q, prefs, profile)
    if q.kind == "multi":
        for i, (value, label) in enumerate(options):
            mark = "✅ " if value in selected else "▫️ "
            kb.button(text=f"{mark}{label}", callback_data=QCb(a=QAction.TOGGLE, v=i))
    elif q.kind == "single":
        for i, (value, label) in enumerate(options):
            mark = "● " if value in selected else ""
            kb.button(text=f"{mark}{label}", callback_data=QCb(a=QAction.PICK, v=i))
    columns = 1 if q.id == "roles" else 2
    sizes = [columns] * ((len(options) + columns - 1) // columns) if options else []
    extra: list[int] = []
    if q.kind == "multi":
        kb.button(text="Done ➡️", callback_data=QCb(a=QAction.DONE))
        extra.append(1)
    if q.allow_custom:
        kb.button(
            text="➕ Add my own" if q.kind == "multi" else "✍️ Other…",
            callback_data=QCb(a=QAction.CUSTOM),
        )
        extra.append(1)
    if q.kind == "text" and current:
        kb.button(text="✔️ Keep current", callback_data=QCb(a=QAction.KEEP))
        extra.append(1)
    kb.button(text="⬅️ Back", callback_data=QCb(a=QAction.BACK))
    kb.button(text="Skip ⏭", callback_data=QCb(a=QAction.SKIP))
    extra.append(2)
    kb.adjust(*sizes, *extra)
    return "\n".join(lines), kb.as_markup()


SIZE_LABEL = {"startup": "Startup", "mid_size": "Mid-size", "mnc": "MNC", "any": "Any"}
TYPE_LABEL = {
    "full_time": "Full-time",
    "internship": "Internship",
    "contract": "Contract",
    "part_time": "Part-time",
    "freelance": "Freelance",
}
MODE_LABEL = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def plan_text(prefs: Preferences, sources: list[str], *, saved: bool = False) -> str:
    salary = prefs.min_salary.raw if prefs.min_salary else "open"
    where = ", ".join(prefs.locations) or "anywhere in " + prefs.home_country
    if prefs.willing_to_relocate:
        where += " (open to relocating)"
    avoid = [*prefs.excluded_companies, *prefs.excluded_keywords]
    lines = [
        "🗺 <b>Your search plan</b>" if not saved else "⚙️ <b>Your preferences</b>",
        "",
        f"🎯 <b>Roles:</b> {esc(', '.join(prefs.target_roles) or 'from your resume')}",
        f"📈 <b>Level:</b> {LEVEL_LABEL.get(prefs.experience_level, prefs.experience_level)}",
        f"💼 <b>Type:</b> {', '.join(TYPE_LABEL[t] for t in prefs.job_types)}",
        f"🏠 <b>Mode:</b> {', '.join(MODE_LABEL[m] for m in prefs.work_modes)}",
        f"📍 <b>Where:</b> {esc(where)}",
        f"💰 <b>Minimum pay:</b> {esc(salary)}",
        f"🏢 <b>Companies:</b> {', '.join(SIZE_LABEL[s] for s in prefs.company_size)}",
    ]
    if prefs.must_have_keywords:
        lines.append(f"✅ <b>Must have:</b> {esc(', '.join(prefs.must_have_keywords))}")
    if avoid:
        lines.append(f"🚫 <b>Avoid:</b> {esc(', '.join(avoid))}")
    lines += [
        "",
        f"🔎 <b>Sources:</b> {esc(', '.join(sources))}",
        "🧹 Duplicates, stale posts (14+ days) and likely scams are removed.",
        f"📬 <b>Daily digest:</b> up to {prefs.jobs_per_digest} new matches at "
        f"{prefs.digest_time}, new jobs only.",
    ]
    return "\n".join(lines)


def plan_keyboard(*, saved: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💾 Save & search now" if saved else "🚀 Start search",
        callback_data=QCb(a=QAction.START),
    )
    kb.button(text="✏️ Edit", callback_data=QCb(a=QAction.EDIT, v=-1))
    kb.adjust(1, 1)
    return kb.as_markup()


SHORT = {
    "roles": "Roles",
    "job_types": "Job type",
    "work_modes": "Work mode",
    "locations": "Locations",
    "relocate": "Relocation",
    "level": "Level",
    "salary": "Salary",
    "joining": "Joining date",
    "visa": "Visa",
    "exclude": "Exclusions",
    "keywords": "Must-haves / deal-breakers",
    "company_size": "Company type",
    "per_digest": "Jobs per digest",
    "digest_time": "Digest time",
}


def edit_menu(prefs: Preferences, profile: CandidateProfile) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    order = [q.id for q in QUESTIONS]
    for q in applicable(prefs, profile):
        kb.button(text=SHORT[q.id], callback_data=QCb(a=QAction.EDIT, v=order.index(q.id)))
    kb.button(text="⬅️ Back to plan", callback_data=QCb(a=QAction.PLAN))
    kb.adjust(2)
    return kb.as_markup()


def question_by_index(i: int) -> Question:
    return QUESTIONS[i]


__all__ = [
    "BY_ID",
    "QAction",
    "QCb",
    "edit_menu",
    "plan_keyboard",
    "plan_text",
    "question_by_index",
    "render_question",
]
