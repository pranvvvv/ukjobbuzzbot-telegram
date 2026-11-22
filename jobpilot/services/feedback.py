"""Learning from feedback (spec 8.5): simple, explainable weights, not a black box.

- Save / More like this → boost the job title's specific terms in stage-1 scoring.
- Not interested (role) → penalise those terms.
- Not interested (location) ×3 → stop expanding to other cities (relocation off).
- Not interested (seniority) ×2 → tighter seniority filter.
- Not interested (company) → offer to exclude the company (user confirms).
Every change adds a plain-English note shown in /preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Feedback, MatchStatus
from jobpilot.matching.normalize import norm_title
from jobpilot.services.matches import MatchService, MatchView
from jobpilot.services.preferences import FeedbackWeights, PreferenceService
from jobpilot.services.profile import ProfileService

GENERIC_TITLE_TERMS = {
    "engineer",
    "developer",
    "software",
    "senior",
    "junior",
    "lead",
    "staff",
    "principal",
    "intern",
    "internship",
    "associate",
    "i",
    "ii",
    "iii",
    "iv",
    "manager",
    "specialist",
    "the",
    "and",
    "of",
    "for",
    "with",
    "remote",
    "india",
    "hybrid",
    "sr",
    "jr",
    "full",
    "time",
    "development",
    "team",
    "role",
    "position",
    "graduate",
    "fresher",
    "trainee",
    "analyst",
}


class FeedbackAction(StrEnum):
    SAVE = "save"
    UNSAVE = "unsave"
    DISMISS = "dismiss"
    MORE = "more"
    APPLIED = "applied"


class DismissReason(StrEnum):
    ROLE = "role"
    LOCATION = "location"
    SALARY = "salary"
    COMPANY = "company"
    SENIORITY = "seniority"
    OTHER = "other"


@dataclass(slots=True)
class FeedbackEffect:
    view: MatchView | None
    note: str | None = None
    ask_exclude_company: str | None = None


def title_terms(title: str) -> list[str]:
    return [t for t in norm_title(title).split() if t not in GENERIC_TITLE_TERMS and len(t) > 1]


def _bump(table: dict[str, float], terms: list[str], step: float, cap: float) -> None:
    for t in terms:
        table[t] = round(min(cap, table.get(t, 0.0) + step), 3)


class FeedbackService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        matches: MatchService,
        profiles: ProfileService,
        prefs: PreferenceService,
    ) -> None:
        self._sessions = sessions
        self._matches = matches
        self._profiles = profiles
        self._prefs = prefs

    async def _weights(self, user_id: int) -> tuple[int, FeedbackWeights] | None:
        active = await self._profiles.active(user_id)
        if active is None:
            return None
        loaded = await self._prefs.load(user_id, active[0])
        return (active[0], loaded[1]) if loaded else None

    async def _log(self, user_id: int, job_id: int, action: str, reason: str | None) -> None:
        async with self._sessions.begin() as session:
            session.add(
                Feedback(
                    user_id=user_id,
                    job_id=job_id,
                    action=action,
                    reason=reason,
                    created_at=utcnow(),
                )
            )

    async def record(
        self,
        user_id: int,
        match_id: int,
        action: FeedbackAction,
        reason: DismissReason | None = None,
    ) -> FeedbackEffect:
        status = {
            FeedbackAction.SAVE: MatchStatus.SAVED,
            FeedbackAction.UNSAVE: MatchStatus.SENT,
            FeedbackAction.DISMISS: MatchStatus.DISMISSED,
            FeedbackAction.APPLIED: MatchStatus.APPLIED,
        }.get(action)
        view = (
            await self._matches.set_status(match_id, user_id, status)
            if status
            else await self._matches.get(match_id, user_id)
        )
        if view is None:
            return FeedbackEffect(None)
        await self._log(user_id, view.job_id, action.value, reason.value if reason else None)
        ctx = await self._weights(user_id)
        if ctx is None:
            return FeedbackEffect(view)
        profile_id, w = ctx
        effect = FeedbackEffect(view)
        terms = title_terms(view.title)
        if action in {FeedbackAction.SAVE, FeedbackAction.MORE} and terms:
            _bump(w.boost_terms, terms, 0.08 if action is FeedbackAction.MORE else 0.05, 0.15)
            for t in terms:
                w.penalty_terms.pop(t, None)
            effect.note = f"I'll favour roles like “{view.title}”."
        elif action is FeedbackAction.DISMISS and reason is not None:
            w.dismiss_counts[reason.value] = w.dismiss_counts.get(reason.value, 0) + 1
            count = w.dismiss_counts[reason.value]
            if reason is DismissReason.ROLE and terms:
                _bump(w.penalty_terms, terms, 0.1, 0.3)
                for t in terms:
                    w.boost_terms.pop(t, None)
                effect.note = f"Showing fewer roles like “{view.title}”."
            elif reason is DismissReason.LOCATION and count >= 3 and not w.location_strict:
                w.location_strict = True
                effect.note = "Tightened locations: only your preferred cities from now on."
            elif reason is DismissReason.SENIORITY and count >= 2 and not w.seniority_strict:
                w.seniority_strict = True
                effect.note = "Tightened the seniority filter to your level."
            elif reason is DismissReason.SALARY and count >= 2:
                effect.note = "Tip: set a minimum salary in /preferences to filter these out."
            elif reason is DismissReason.COMPANY:
                effect.ask_exclude_company = view.company
        if effect.note and effect.note not in w.notes:
            w.notes = [*w.notes[-9:], effect.note]
        await self._prefs.save_weights(user_id, profile_id, w)
        return effect

    async def exclude_company(self, user_id: int, company: str) -> bool:
        active = await self._profiles.active(user_id)
        if active is None:
            return False
        loaded = await self._prefs.load(user_id, active[0])
        if loaded is None:
            return False
        prefs, weights = loaded
        if company not in prefs.excluded_companies:
            prefs = prefs.model_copy(
                update={"excluded_companies": [*prefs.excluded_companies, company]}
            )
            await self._prefs.save(user_id, active[0], prefs)
            weights.notes = [*weights.notes[-9:], f"Excluded {company} at your request."]
            await self._prefs.save_weights(user_id, active[0], weights)
        return True
