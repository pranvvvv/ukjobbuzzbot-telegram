"""Turn profile + preferences into a small, diverse set of search queries (spec 7)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobpilot.core.logging import get_logger
from jobpilot.llm.base import LLMClient, LLMError, dumps_compact
from jobpilot.llm.prompting import fence, load_prompt
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.preferences import Preferences
from jobpilot.sources.base import SearchQuery

log = get_logger(__name__)


class QueryItem(BaseModel):
    keywords: str
    location: str | None
    remote: bool


class QueryPlan(BaseModel):
    queries: list[QueryItem] = Field(max_length=12)


_LEVEL_WORD = {"intern": "intern", "fresher": "fresher", "junior": "junior"}


def fallback_queries(
    profile: CandidateProfile, prefs: Preferences, limit: int
) -> list[SearchQuery]:
    """Deterministic: roles × (cities | remote), level word for early-career, deduped."""
    roles = prefs.target_roles or profile.inferred_target_roles or ["software engineer"]
    word = _LEVEL_WORD.get(prefs.experience_level)
    cities: list[str | None] = (
        list(prefs.locations[:2])
        if any(m in prefs.work_modes for m in ("onsite", "hybrid"))
        else []
    )
    out: list[SearchQuery] = []
    for role in roles[:3]:
        if not word or word in role.lower():
            kw = role
        else:  # "Backend Developer intern" / "junior Backend Developer" read like real searches
            kw = f"{role} {word}" if word == "intern" else f"{word} {role}"
        if "internship" in prefs.job_types and "intern" not in kw.lower():
            out.append(SearchQuery(f"{role} intern", cities[0] if cities else None))
        for city in cities or [None]:
            out.append(SearchQuery(kw, city))
        if "remote" in prefs.work_modes:
            out.append(SearchQuery(role, None, remote=True))
    seen: set[tuple[str, str | None, bool]] = set()
    unique = []
    for q in out:
        key = (q.keywords.lower(), q.location, q.remote)
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[:limit]


async def build_queries(
    llm: LLMClient, profile: CandidateProfile, prefs: Preferences, *, limit: int
) -> tuple[list[SearchQuery], bool]:
    """(queries, used_ai). Falls back to rules if the LLM is unavailable or unhelpful."""
    candidate = dumps_compact(
        {
            "target_roles": prefs.target_roles,
            "level": prefs.experience_level,
            "top_skills": profile.top_skills(8),
            "job_types": prefs.job_types,
            "work_modes": prefs.work_modes,
            "preferred_locations": prefs.locations,
            "country": prefs.home_country,
        }
    )
    try:
        result = await llm.complete_json(
            QueryPlan,
            load_prompt("search_queries").render(
                max_queries=str(limit), candidate=fence("candidate", candidate)
            ),
            max_output_tokens=800,
        )
        queries = [
            SearchQuery(q.keywords.strip()[:60], q.location, q.remote)
            for q in result.value.queries
            if q.keywords.strip()
        ][:limit]
        if queries:
            return queries, True
    except LLMError as exc:
        log.info("queries.fallback", reason=str(exc))
    return fallback_queries(profile, prefs, limit), False
