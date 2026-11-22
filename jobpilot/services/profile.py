"""Profile use-cases: analyse a resume, save drafts, confirm, correct, archive, delete.

AI first, rules as fallback: if the LLM is unavailable (no key, quota, outage) or keeps
returning invalid JSON, the rule-based parser produces a usable profile and the card
says so. Drafts stay inactive until the user taps "Looks right".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.logging import get_logger
from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Match, MatchStatus, Preference, Profile, User
from jobpilot.db.repo import ProfileRepo
from jobpilot.llm.base import LLMClient, LLMError, dumps_compact
from jobpilot.llm.prompting import fence, load_prompt
from jobpilot.matching.locations import normalize_city
from jobpilot.matching.skills import category_of, normalize_skill
from jobpilot.resume.profile import finalize, heuristic_extract, looks_like_resume
from jobpilot.resume.schema import CandidateProfile, ProfileExtraction, ResumeCheck

log = get_logger(__name__)

CLASSIFIER_CHARS = 4000


class NotAResumeError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Analysis:
    profile: CandidateProfile
    used_ai: bool


def _today() -> date:
    return utcnow().date()


class ProfileService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], llm: LLMClient) -> None:
        self._sessions = sessions
        self._llm = llm

    # ------------------------------------------------------------ analyse
    async def analyze(self, text: str) -> Analysis:
        today = _today()
        try:
            check = await self._llm.complete_json(
                ResumeCheck,
                load_prompt("classify_resume").render(
                    document=fence("document", text[:CLASSIFIER_CHARS])
                ),
                max_output_tokens=200,
            )
            if not check.value.is_resume:
                raise NotAResumeError(check.value.reason)
            result = await self._llm.complete_json(
                ProfileExtraction,
                load_prompt("extract_profile").render(
                    today=today.isoformat(), resume=fence("resume", text)
                ),
                max_output_tokens=4096,
            )
            return Analysis(finalize(result.value, text, today=today), used_ai=True)
        except LLMError as exc:
            log.warning("profile.ai_unavailable_using_rules", error=str(exc))
        if not looks_like_resume(text):
            raise NotAResumeError("it doesn't have the usual resume sections")
        extraction = heuristic_extract(text, today)
        return Analysis(
            finalize(extraction, text, today=today, parsed_with_ai=False), used_ai=False
        )

    async def correct(self, profile_id: int, correction: str) -> CandidateProfile:
        """Apply a free-text correction ("I'm in Pune, add Docker") to a draft/active profile."""
        async with self._sessions.begin() as session:
            row = await session.get(Profile, profile_id)
            if row is None:
                raise LookupError(profile_id)
            current = CandidateProfile.model_validate(row.profile_json)
            today = _today()
            try:
                result = await self._llm.complete_json(
                    ProfileExtraction,
                    load_prompt("fix_profile").render(
                        profile=fence(
                            "profile",
                            dumps_compact(
                                current.model_dump(include=set(ProfileExtraction.model_fields))
                            ),
                        ),
                        correction=fence("correction", correction),
                        resume=fence("resume", row.raw_resume_text),
                    ),
                    max_output_tokens=4096,
                )
                updated = finalize(
                    result.value, row.raw_resume_text, today=today, extra_evidence=correction
                )
            except LLMError as exc:
                log.warning("profile.fix_without_ai", error=str(exc))
                updated = apply_simple_correction(current, correction)
            row.profile_json = updated.model_dump(mode="json")
            return updated

    # ------------------------------------------------------------ storage
    async def save_draft(self, user_id: int, text: str, profile: CandidateProfile) -> int:
        async with self._sessions.begin() as session:
            version = await ProfileRepo(session).next_version(user_id)
            row = Profile(
                user_id=user_id,
                version=version,
                raw_resume_text=text,
                resume_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                profile_json=profile.model_dump(mode="json"),
                is_active=False,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def get(self, profile_id: int) -> CandidateProfile | None:
        async with self._sessions() as session:
            row = await session.get(Profile, profile_id)
            return CandidateProfile.model_validate(row.profile_json) if row else None

    async def activate(self, user_id: int, profile_id: int) -> int:
        """Make the draft the active profile; unsent scores from older versions are cleared
        so jobs get re-scored against the new profile (sent/saved history is kept)."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(Profile).where(Profile.user_id == user_id).values(is_active=False)
            )
            row = await session.get(Profile, profile_id)
            if row is None or row.user_id != user_id:
                raise LookupError(profile_id)
            row.is_active = True
            await session.execute(
                delete(Match).where(Match.user_id == user_id, Match.status == MatchStatus.NEW)
            )
            return row.version

    async def active(self, user_id: int) -> tuple[int, int, CandidateProfile] | None:
        """(profile_id, version, profile) for the user's active profile."""
        async with self._sessions() as session:
            row = await ProfileRepo(session).active_for(user_id)
            if row is None:
                return None
            return row.id, row.version, CandidateProfile.model_validate(row.profile_json)

    async def archive(self, user_id: int, *, reset_history: bool) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(Profile).where(Profile.user_id == user_id).values(is_active=False)
            )
            user = await session.get(User, user_id)
            if user is not None:
                user.digest_enabled = False
                user.next_digest_at = None
            if reset_history:
                await session.execute(delete(Match).where(Match.user_id == user_id))

    async def delete_everything(self, user_id: int) -> None:
        """/deleteme: the user row cascades to profiles, prefs, matches, feedback, runs."""
        async with self._sessions.begin() as session:
            await session.execute(delete(Preference).where(Preference.user_id == user_id))
            await session.execute(delete(User).where(User.telegram_id == user_id))

    async def latest_profile_ids(self, user_id: int) -> list[int]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Profile.id).where(Profile.user_id == user_id).order_by(Profile.version)
            )
            return list(rows.scalars())


# ------------------------------------------------------------------ no-AI corrections
_ADD = re.compile(r"\b(?:add|also know|i know|include)\s+(?P<items>[^.;\n]+)", re.I)
_REMOVE = re.compile(r"\b(?:remove|drop|delete|i don'?t know)\s+(?P<items>[^.;\n]+)", re.I)
_LOCATION = re.compile(
    r"\b(?:i'?m in|i am in|based in|location(?: is|:)?|live in|living in)\s+(?P<loc>[A-Za-z .-]+)",
    re.I,
)
_NAME = re.compile(r"\b(?:my name is|name(?: is|:))\s+(?P<name>[A-Za-z .'-]+)", re.I)


def _split_items(raw: str) -> list[str]:
    raw = re.sub(r"\b(to|from)\s+(my\s+)?skills?\b", "", raw, flags=re.I)
    return [p.strip(" .") for p in re.split(r",|\band\b|&|/", raw) if p.strip(" .")]


def apply_simple_correction(profile: CandidateProfile, correction: str) -> CandidateProfile:
    """Understands 'add X, Y', 'remove Z', 'I'm in <city>', 'my name is …' without an LLM."""
    data = profile.model_dump()
    skills = data["skills"]
    for m in _ADD.finditer(correction):
        for item in _split_items(m.group("items")):
            name = normalize_skill(item)
            group = category_of(name) or "tools"
            if name not in skills[group]:
                skills[group].append(name)
    for m in _REMOVE.finditer(correction):
        targets = {normalize_skill(i).lower() for i in _split_items(m.group("items"))}
        for group in skills:
            skills[group] = [s for s in skills[group] if s.lower() not in targets]
    if loc := _LOCATION.search(correction):
        data["location_current"] = normalize_city(loc.group("loc"))
        data["gaps_or_flags"] = [f for f in data["gaps_or_flags"] if "location" not in f]
    if name_match := _NAME.search(correction):
        data["full_name"] = name_match.group("name").strip()
    return CandidateProfile.model_validate(data)
