"""Job preferences, the clarifying-question bank, and input parsers (spec 4.4).

Question selection is rule-based (deterministic, testable): each question has a
`when(prefs, profile)` condition, evaluated on the *current* answers, so e.g. the visa
question appears only after a non-home-country location is chosen.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Preference
from jobpilot.matching.locations import find_country, normalize_city
from jobpilot.resume.schema import CandidateProfile, Seniority

JobType = Literal["full_time", "internship", "contract", "part_time", "freelance"]
WorkMode = Literal["remote", "hybrid", "onsite"]
CompanySize = Literal["startup", "mid_size", "mnc", "any"]
Joining = Literal["immediate", "15_days", "30_days", "60_days", "90_days"]


class SalaryPref(BaseModel):
    amount: float
    currency: str
    period: Literal["year", "month"]
    raw: str

    def annual(self) -> float:
        return self.amount * (12 if self.period == "month" else 1)


class FeedbackWeights(BaseModel):
    """Explainable learning from feedback (spec 8.5); stored in preferences.weights_json."""

    boost_terms: dict[str, float] = Field(default_factory=dict)
    penalty_terms: dict[str, float] = Field(default_factory=dict)
    dismiss_counts: dict[str, int] = Field(default_factory=dict)
    location_strict: bool = False
    seniority_strict: bool = False
    notes: list[str] = Field(default_factory=list)


class Preferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    job_types: list[JobType] = Field(default_factory=lambda: list[JobType](["full_time"]))
    work_modes: list[WorkMode] = Field(
        default_factory=lambda: list[WorkMode](["remote", "hybrid", "onsite"])
    )
    locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool = False
    experience_level: Seniority = "fresher"
    min_salary: SalaryPref | None = None
    joining: Joining | None = None
    needs_visa: bool | None = None
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    must_have_keywords: list[str] = Field(default_factory=list)
    company_size: list[CompanySize] = Field(default_factory=lambda: list[CompanySize](["any"]))
    jobs_per_digest: int = 10
    digest_time: str = "09:00"
    home_country: str = "India"

    def abroad_locations(self) -> list[str]:
        return [
            loc
            for loc in self.locations
            if (c := find_country(loc)) is not None and c != self.home_country
        ]


def prefill(profile: CandidateProfile, *, jobs_per_digest: int, digest_time: str) -> Preferences:
    """Everything the resume already answers, so we only ask what it couldn't."""
    job_types: list[JobType] = ["internship"] if profile.seniority == "intern" else ["full_time"]
    if profile.is_student and profile.seniority != "intern":
        job_types = ["full_time", "internship"]
    return Preferences(
        target_roles=profile.inferred_target_roles[:3],
        job_types=job_types,
        locations=[normalize_city(profile.location_current)] if profile.location_current else [],
        experience_level=profile.seniority,
        jobs_per_digest=jobs_per_digest,
        digest_time=digest_time,
    )


# ------------------------------------------------------------------ question bank
Kind = Literal["multi", "single", "text"]
Cond = Callable[[Preferences, CandidateProfile], bool]


def _always(_p: Preferences, _c: CandidateProfile) -> bool:
    return True


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    kind: Kind
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    optional: bool = False
    allow_custom: bool = False  # multi/single: "Add my own" / "Other" free text
    when: Cond = _always
    hint: str = ""


def _needs_location(p: Preferences, _c: CandidateProfile) -> bool:
    return any(m in p.work_modes for m in ("onsite", "hybrid"))


def _level_unclear(_p: Preferences, c: CandidateProfile) -> bool:
    flags = " ".join(c.gaps_or_flags).lower()
    return (not c.parsed_with_ai) or any(w in flags for w in ("date", "graduat", "experience"))


QUESTIONS: tuple[Question, ...] = (
    Question(
        "roles",
        "Which roles should I target?",
        "multi",
        allow_custom=True,
        hint="Tap to toggle. Pre-filled from your resume.",
    ),
    Question(
        "job_types",
        "What type of job?",
        "multi",
        options=[
            ("full_time", "Full-time"),
            ("internship", "Internship"),
            ("contract", "Contract"),
            ("part_time", "Part-time"),
            ("freelance", "Freelance"),
        ],
    ),
    Question(
        "work_modes",
        "Which work modes are OK?",
        "multi",
        options=[("remote", "Remote"), ("hybrid", "Hybrid"), ("onsite", "On-site")],
    ),
    Question(
        "locations",
        "Preferred cities or countries?",
        "text",
        when=_needs_location,
        hint="Type them separated by commas, e.g. Hyderabad, Bengaluru, Pune.",
    ),
    Question(
        "relocate",
        "Willing to relocate within the country for the right role?",
        "single",
        options=[("yes", "Yes"), ("no", "No")],
        when=_needs_location,
    ),
    Question(
        "level",
        "Which experience level should I target?",
        "single",
        options=[
            ("intern", "Intern"),
            ("fresher", "Fresher"),
            ("junior", "Junior (0-2 yrs)"),
            ("mid", "Mid (2-5 yrs)"),
            ("senior", "Senior (5+ yrs)"),
            ("lead", "Lead"),
        ],
        when=_level_unclear,
    ),
    Question(
        "salary",
        "Minimum expected salary or stipend?",
        "text",
        optional=True,
        hint="e.g. 6 LPA, 25k per month, $60k. Tap Skip to leave it open.",
    ),
    Question(
        "joining",
        "When can you join?",
        "single",
        options=[
            ("immediate", "Immediately"),
            ("15_days", "Within 15 days"),
            ("30_days", "30 days"),
            ("60_days", "60 days"),
            ("90_days", "90 days"),
        ],
        optional=True,
    ),
    Question(
        "visa",
        "Will you need visa sponsorship for jobs abroad?",
        "single",
        options=[("yes", "Yes"), ("no", "No")],
        when=lambda p, _c: bool(p.abroad_locations()),
    ),
    Question(
        "exclude",
        "Any companies or industries to exclude?",
        "text",
        optional=True,
        hint="e.g. Byju's, gambling, crypto. Tap Skip for none.",
    ),
    Question(
        "keywords",
        "Any must-haves or deal-breakers?",
        "text",
        optional=True,
        hint='e.g. "Python only", "no sales roles", "no night shifts".',
    ),
    Question(
        "company_size",
        "What kind of company?",
        "multi",
        options=[("startup", "Startup"), ("mid_size", "Mid-size"), ("mnc", "MNC"), ("any", "Any")],
    ),
    Question(
        "per_digest",
        "How many jobs per daily digest?",
        "single",
        options=[("5", "5"), ("10", "10"), ("15", "15")],
    ),
    Question(
        "digest_time",
        "What time should the daily digest arrive?",
        "single",
        options=[
            ("07:00", "7 AM"),
            ("09:00", "9 AM"),
            ("12:00", "12 PM"),
            ("18:00", "6 PM"),
            ("21:00", "9 PM"),
        ],
        allow_custom=True,
    ),
)
BY_ID = {q.id: q for q in QUESTIONS}


def applicable(prefs: Preferences, profile: CandidateProfile) -> list[Question]:
    return [q for q in QUESTIONS if q.when(prefs, profile)]


def next_question(
    after: str | None, prefs: Preferences, profile: CandidateProfile
) -> Question | None:
    order = [q.id for q in QUESTIONS]
    start = order.index(after) + 1 if after else 0
    for q in QUESTIONS[start:]:
        if q.when(prefs, profile):
            return q
    return None


def prev_question(before: str, prefs: Preferences, profile: CandidateProfile) -> Question | None:
    order = [q.id for q in QUESTIONS]
    for q in reversed(QUESTIONS[: order.index(before)]):
        if q.when(prefs, profile):
            return q
    return None


def position(qid: str, prefs: Preferences, profile: CandidateProfile) -> tuple[int, int]:
    items = [q.id for q in applicable(prefs, profile)]
    return (items.index(qid) + 1 if qid in items else 1), len(items)


def options_for(
    q: Question, prefs: Preferences, profile: CandidateProfile
) -> list[tuple[str, str]]:
    if q.id == "roles":
        roles = list(dict.fromkeys([*prefs.target_roles, *profile.inferred_target_roles]))
        return [(r, r) for r in roles]
    return q.options


def selected_values(q: Question, prefs: Preferences) -> list[str]:
    values: dict[str, list[str]] = {
        "roles": prefs.target_roles,
        "job_types": list(prefs.job_types),
        "work_modes": list(prefs.work_modes),
        "company_size": list(prefs.company_size),
        "relocate": ["yes" if prefs.willing_to_relocate else "no"],
        "level": [prefs.experience_level],
        "joining": [prefs.joining] if prefs.joining else [],
        "visa": [] if prefs.needs_visa is None else ["yes" if prefs.needs_visa else "no"],
        "per_digest": [str(prefs.jobs_per_digest)],
        "digest_time": [prefs.digest_time],
    }
    return values.get(q.id, [])


def apply_choice(q: Question, values: list[str], prefs: Preferences) -> Preferences:
    """Apply a multi/single selection; validation via the Pydantic model."""
    data = prefs.model_dump()
    first = values[0] if values else None
    match q.id:
        case "roles":
            data["target_roles"] = values
        case "job_types":
            data["job_types"] = values or ["full_time"]
        case "work_modes":
            data["work_modes"] = values or ["remote", "hybrid", "onsite"]
        case "company_size":
            data["company_size"] = values or ["any"]
        case "relocate":
            data["willing_to_relocate"] = first == "yes"
        case "level":
            data["experience_level"] = first or data["experience_level"]
        case "joining":
            data["joining"] = first
        case "visa":
            data["needs_visa"] = first == "yes"
        case "per_digest":
            data["jobs_per_digest"] = int(first or 10)
        case "digest_time":
            data["digest_time"] = first or data["digest_time"]
    return Preferences.model_validate(data)


class InputError(ValueError):
    """User-facing parse error."""


def apply_text(q: Question, text: str, prefs: Preferences) -> Preferences:
    data = prefs.model_dump()
    match q.id:
        case "roles":
            extra = parse_list(text)
            data["target_roles"] = list(dict.fromkeys([*prefs.target_roles, *extra]))
        case "locations":
            data["locations"] = [normalize_city(x) for x in parse_list(text)]
        case "salary":
            salary = parse_salary(text, "INR" if prefs.home_country == "India" else "USD")
            if salary is None:
                raise InputError("I couldn't read that amount. Try e.g. 6 LPA or 25k per month.")
            data["min_salary"] = salary.model_dump()
        case "exclude":
            data["excluded_companies"] = parse_list(text)
        case "keywords":
            must, avoid = parse_keywords(text)
            data["must_have_keywords"], data["excluded_keywords"] = must, avoid
        case "digest_time":
            parsed = parse_time(text)
            if parsed is None:
                raise InputError("Please send a time like 08:30 or 7pm.")
            data["digest_time"] = parsed
        case _:
            raise InputError("Please use the buttons for this one.")
    return Preferences.model_validate(data)


def skip(q: Question, prefs: Preferences) -> Preferences:
    data = prefs.model_dump()
    resets: dict[str, tuple[str, object]] = {
        "salary": ("min_salary", None),
        "joining": ("joining", None),
        "exclude": ("excluded_companies", []),
        "visa": ("needs_visa", None),
    }
    if q.id in resets:
        key, value = resets[q.id]
        data[key] = value
    if q.id == "keywords":
        data["must_have_keywords"], data["excluded_keywords"] = [], []
    return Preferences.model_validate(data)


# ------------------------------------------------------------------ parsers
def parse_list(text: str) -> list[str]:
    items = re.split(r"[,\n;]+|\s+/\s+", text)
    return list(dict.fromkeys(i.strip(" .") for i in items if i.strip(" .")))


_NEGATIVE = re.compile(r"^(no|not|avoid|exclude|without|never|don'?t want)\s+", re.I)


def parse_keywords(text: str) -> tuple[list[str], list[str]]:
    """'Python only, no sales roles, avoid nights' → (['Python'], ['sales', 'nights'])."""
    must: list[str] = []
    avoid: list[str] = []
    for item in parse_list(text):
        if _NEGATIVE.match(item):
            term = _NEGATIVE.sub("", item)
            term = re.sub(r"\b(roles?|jobs?|positions?)\b", "", term, flags=re.I).strip()
            if term:
                avoid.append(term)
        else:
            term = re.sub(r"\s+only$", "", item, flags=re.I).strip()
            if term:
                must.append(term)
    return must, avoid


_CURRENCIES = {
    "₹": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}


def parse_salary(text: str, default_currency: str = "INR") -> SalaryPref | None:
    raw = text.strip()
    t = raw.lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(lpa|lakhs?|lacs?|l\b|k\b|thousand|cr|crore|m\b)?", t)
    if not m:
        return None
    amount = float(m.group(1))
    unit = (m.group(2) or "").strip()
    currency = next((code for sym, code in _CURRENCIES.items() if sym in t), default_currency)
    period: Literal["year", "month"] = (
        "month"
        if re.search(r"per month|/\s*month|/\s*mo\b|\bpm\b|monthly|stipend|a month", t)
        else "year"
    )
    if unit in {"lpa", "lakh", "lakhs", "lac", "lacs", "l"}:
        amount *= 100_000
        currency = "INR"
        period = "month" if period == "month" and unit != "lpa" else "year"
    elif unit in {"k", "thousand"}:
        amount *= 1_000
    elif unit in {"cr", "crore"}:
        amount *= 10_000_000
    elif unit == "m":
        amount *= 1_000_000
    if amount <= 0:
        return None
    return SalaryPref(amount=amount, currency=currency, period=period, raw=raw)


def parse_time(text: str) -> str | None:
    t = text.strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


# ------------------------------------------------------------------ storage
class PreferenceService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, user_id: int, profile_id: int, prefs: Preferences) -> None:
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    select(Preference).where(
                        Preference.user_id == user_id, Preference.profile_id == profile_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    Preference(
                        user_id=user_id,
                        profile_id=profile_id,
                        prefs_json=prefs.model_dump(mode="json"),
                        weights_json={},
                    )
                )
            else:
                row.prefs_json = prefs.model_dump(mode="json")
                row.updated_at = utcnow()

    async def load(
        self, user_id: int, profile_id: int
    ) -> tuple[Preferences, FeedbackWeights] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Preference).where(
                        Preference.user_id == user_id, Preference.profile_id == profile_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return (
                Preferences.model_validate(row.prefs_json),
                FeedbackWeights.model_validate(row.weights_json or {}),
            )

    async def save_weights(self, user_id: int, profile_id: int, weights: FeedbackWeights) -> None:
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    select(Preference).where(
                        Preference.user_id == user_id, Preference.profile_id == profile_id
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.weights_json = weights.model_dump(mode="json")
