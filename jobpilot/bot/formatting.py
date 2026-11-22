"""All user-facing text. HTML parse mode: every dynamic value goes through `esc`."""

from __future__ import annotations

import html
from datetime import datetime

from jobpilot.bot.commands import available_commands
from jobpilot.core.timeutil import format_local, utcnow
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.matches import MatchView
from jobpilot.services.users import UserStatus

TELEGRAM_MESSAGE_LIMIT = 4096


def esc(value: object) -> str:
    """Escape dynamic text for Telegram HTML (`<`, `>`, `&`; quotes are safe outside attrs)."""
    return html.escape(str(value), quote=False)


def utf16_len(text: str) -> int:
    """Telegram measures message length in UTF-16 code units, not Python characters."""
    return len(text.encode("utf-16-le")) // 2


def welcome_text(first_name: str | None) -> str:
    hello = f"👋 Hi {esc(first_name)}!" if first_name else "👋 Hi!"
    return (
        f"{hello} I'm <b>JobPilot</b>, your personal job-search agent.\n\n"
        "<b>How it works</b>\n"
        "1. Send me your resume.\n"
        "2. I show you what I understood and ask a few quick questions.\n"
        "3. I search legitimate job sources and send the best matches, each with an "
        "apply link and why it fits. Then a daily digest of <i>new</i> matches only.\n\n"
        "🔒 <b>Privacy:</b> your resume stays on this computer. It's only sent to the AI "
        "model to read it and score jobs. I never apply on your behalf, and /deleteme "
        "wipes everything.\n\n"
        "📄 Send your resume as <b>PDF</b> or <b>DOCX</b>, or paste it as text."
    )


def status_text(status: UserStatus, first_name: str | None) -> str:
    name = f", {esc(first_name)}" if first_name else ""
    if not status.has_profile:
        return (
            f"Welcome back{name}. I don't have your resume yet.\n\n"
            "📄 Send it as <b>PDF</b> or <b>DOCX</b>, or paste it as text."
        )
    if status.digest_enabled and status.next_digest_at is not None:
        digest = f"on, next {esc(format_local(status.next_digest_at, status.timezone))}"
    else:
        digest = f"paused (usual time {status.digest_time.strftime('%H:%M')})"
    return (
        f"Welcome back{name}.\n\n"
        f"📄 Profile: on file (version {status.profile_version})\n"
        f"📬 Daily digest: {digest}\n"
        f"🌍 Timezone: {esc(status.timezone)}"
    )


def help_text() -> str:
    lines = [f"/{c.name}: {esc(c.description)}" for c in available_commands()]
    return (
        "<b>JobPilot commands</b>\n"
        + "\n".join(lines)
        + "\n\nTip: tap 💾 Save, 🙅 Not interested or ➕ More like this on job cards and "
        "I'll learn what you like."
    )


NOT_YET_AVAILABLE = "That isn't switched on yet. It arrives in an upcoming build."
UNKNOWN_INPUT = "I didn't catch that. Send /start for your status or /help for commands."


# ------------------------------------------------------------------ profile card
LEVEL_LABEL = {
    "intern": "Intern",
    "fresher": "Fresher",
    "junior": "Junior",
    "mid": "Mid-level",
    "senior": "Senior",
    "lead": "Lead",
}


def profile_card(profile: CandidateProfile, *, draft: bool = True) -> str:
    years = profile.total_experience_years
    level = LEVEL_LABEL.get(profile.seniority, profile.seniority)
    exp = f"{years:g} yrs experience" if years else "no full-time experience yet"
    if profile.internship_months:
        exp += f" + {profile.internship_months} months of internships"
    if profile.is_student:
        exp += " · student"
    lines = [
        "📋 <b>Here's what I understood</b>" if draft else "📋 <b>Your profile</b>",
        "",
        f"👤 {esc(profile.full_name or 'Name not found')}"
        + (f" · 📍 {esc(profile.location_current)}" if profile.location_current else ""),
        f"🎯 <b>Top roles:</b> {esc(', '.join(profile.inferred_target_roles[:4]) or 'none found')}",
        f"📈 <b>Level:</b> {level} ({esc(exp)})",
        f"🛠 <b>Top skills:</b> {esc(', '.join(profile.top_skills(10)) or 'none found')}",
    ]
    if profile.domains:
        lines.append(f"🏷 <b>Domains:</b> {esc(', '.join(profile.domains[:5]))}")
    if profile.projects:
        lines.append(f"🧩 <b>Projects:</b> {esc(', '.join(p.name for p in profile.projects[:4]))}")
    flags = [f for f in profile.gaps_or_flags if "parsed without ai" not in f.lower()]
    if flags:
        lines.append(f"⚠️ <b>Notes:</b> {esc('; '.join(flags[:3]))}")
    if not profile.parsed_with_ai:
        lines += ["", "<i>Read without AI (no AI key set), so please check the details.</i>"]
    if draft:
        lines += ["", "Does this look right?"]
    return "\n".join(lines)


READING_RESUME = "📄 Reading your resume…"
PASTE_RECEIVED = (
    "📥 Got it. If your resume came through in several messages, keep sending. "
    "I'll read it all together in a moment, or tap <b>Done</b>."
)
SEND_RESUME = "📄 Send your resume as <b>PDF</b> or <b>DOCX</b>, or paste it as text."
FIX_PROMPT = (
    "✏️ Tell me what to change, in your own words. For example:\n"
    "• <i>I'm in Bengaluru, not Hyderabad</i>\n"
    "• <i>add Docker and AWS to my skills</i>\n"
    "• <i>target AI engineer roles too</i>"
)
USE_NEW_FOR_RESUME = "You already have a profile. To replace it with a new resume, use /new."
NO_PROFILE_YET = "I don't have your resume yet. " + SEND_RESUME


# ------------------------------------------------------------------ job cards (spec 9)
_SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "SGD": "S$", "CAD": "C$", "AUD": "A$"}
_EMPLOYMENT = {
    "full_time": "Full-time",
    "internship": "Internship",
    "contract": "Contract",
    "part_time": "Part-time",
    "freelance": "Freelance",
}
_MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_salary(
    lo: float | None,
    hi: float | None,
    currency: str | None,
    period: str | None,
    estimated: bool = False,
) -> str:
    if not lo and not hi:
        return "Not disclosed"
    cur = (currency or "").upper()
    sym = _SYMBOL.get(cur, f"{cur} " if cur else "")
    lo, hi = lo or hi, hi or lo
    assert lo is not None
    assert hi is not None
    if cur == "INR" and (period or "year") == "year":
        rng = _num(lo / 1e5) if lo == hi else f"{_num(lo / 1e5)}–{_num(hi / 1e5)}"
        text = f"₹{rng} LPA"
    else:

        def k(v: float) -> str:
            return f"{_num(v / 1000)}k" if v >= 1000 else _num(v)

        rng = k(lo) if lo == hi else f"{k(lo)}–{k(hi)}"
        suffix = {"month": "/month", "hour": "/hr"}.get(period or "year", "/yr")
        text = f"{sym}{rng}{suffix}"
    return f"≈{text} (est.)" if estimated else text


def posted_ago(posted_at: datetime | None, now: datetime | None = None) -> str:
    if posted_at is None:
        return "Posted date unknown"
    days = ((now or utcnow()) - posted_at).days
    if days <= 0:
        return "Posted today"
    return "Posted 1 day ago" if days == 1 else f"Posted {days} days ago"


def score_badge(score: int, basic: bool) -> str:
    if basic:
        return f"⚪ {score}% basic match"
    icon = "🟢" if score >= 80 else "🟡" if score >= 65 else "🟠"
    return f"{icon} {score}% match"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def job_card(
    view: MatchView, source_label: str, credit_url: str | None, now: datetime | None = None
) -> str:
    """HTML card. Dynamic text is escaped *after* truncation, so we never cut inside a tag."""
    place = view.location or ("Remote" if view.remote_type == "remote" else "Location not stated")
    mode = _MODE.get(view.remote_type or "", "")
    if mode and mode.lower() not in place.lower():
        place = f"{place} ({mode})"
    via = (
        f'via <a href="{esc(credit_url)}">{esc(source_label)}</a>'
        if credit_url
        else f"via {esc(source_label)}"
    )
    meta = [f"🏢 {esc(_truncate(view.company, 60))}", f"📍 {esc(_truncate(place, 60))}"]
    if view.employment_type in _EMPLOYMENT:
        meta.append(f"💼 {_EMPLOYMENT[view.employment_type]}")
    salary = format_salary(
        view.salary_min,
        view.salary_max,
        view.currency,
        view.salary_period,
        view.salary_is_estimated,
    )
    lines = [
        f"{score_badge(view.score, view.is_basic)} · <b>{esc(_truncate(view.title, 120))}</b>",
        " · ".join(meta),
        f"💰 {esc(salary)} · 🕒 {posted_ago(view.posted_at, now)} · {via}",
    ]
    if view.why_fit:
        lines += ["", "<b>Why it fits</b>"]
        lines += [f"• {esc(_truncate(w, 160))}" for w in view.why_fit[:3]]
    if view.missing_skills:
        lines.append(f"\n<b>Gaps:</b> {esc(_truncate(', '.join(view.missing_skills[:5]), 200))}")
    if view.pitch:
        lines.append(f"<b>Pitch:</b> {esc(_truncate(view.pitch, 200))}")
    flags = [f.removeprefix("⚠ ") for f in view.red_flags[:3]]
    if flags:
        lines.append(f"⚠️ <b>Check carefully:</b> {esc(_truncate('; '.join(flags), 200))}")
    if view.is_basic:
        lines.append("<i>Basic match: scored without AI.</i>")
    text = "\n".join(lines)
    assert utf16_len(text) <= TELEGRAM_MESSAGE_LIMIT
    return text


def hidden_card(view: MatchView, reason: str | None) -> str:
    why = f" ({esc(reason)})" if reason else ""
    return f"🙅 <s>{esc(_truncate(view.title, 80))}</s> · {esc(view.company)}{why}"
