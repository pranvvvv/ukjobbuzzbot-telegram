"""The search run: sources → dedupe → persist → unseen → filters → scam → stage 1 → stage 2
→ link health → matches. Used by /search and by the daily digest.

Reliability rules (spec 10): one failing source never breaks the run; every run is
recorded with counts, errors, tokens and cost; a per-user lock prevents overlaps; the
LLM being unavailable degrades scoring to "basic match" instead of failing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.logging import bind_context, get_logger
from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Job, Match, MatchStatus, Run, RunType
from jobpilot.llm.base import Embedder, LLMClient, reset_meter, set_meter
from jobpilot.matching.dedupe import dedupe
from jobpilot.matching.filters import apply_filters
from jobpilot.matching.links import LinkChecker
from jobpilot.matching.normalize import LINK_PRIORITY, JobPosting
from jobpilot.matching.scam import assess
from jobpilot.matching.scoring import Scored, apply_basic, rank, stage1, stage2
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.cost import CostGuard
from jobpilot.services.matches import MatchService, MatchView
from jobpilot.services.preferences import FeedbackWeights, Preferences
from jobpilot.services.queries import build_queries
from jobpilot.sources.base import JobSource, QuotaExceededError, SearchQuery
from jobpilot.sources.registry import SourceSet

log = get_logger(__name__)
Progress = Callable[[str], Awaitable[None]]
SOURCE_TIMEOUT = 120.0


class SearchInProgressError(Exception):
    pass


class NotReadyError(Exception):
    """No active profile or saved preferences yet."""


@dataclass
class RunOutcome:
    run_id: int
    kind: RunType
    to_send: list[MatchView] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    llm_scored: int = 0
    used_ai_queries: bool = False
    tokens: int = 0
    cost_usd: float = 0.0
    sources_tried: int = 0

    @property
    def all_sources_failed(self) -> bool:
        return self.sources_tried > 0 and len(self.errors) >= self.sources_tried

    @property
    def basic_only(self) -> bool:
        return self.llm_scored == 0


@dataclass
class SearchDeps:
    sessions: async_sessionmaker[AsyncSession]
    llm: LLMClient
    embedder: Embedder
    sources: SourceSet
    links: LinkChecker
    cost: CostGuard
    matches: MatchService
    load_context: Callable[
        [int], Awaitable[tuple[int, int, CandidateProfile, Preferences, FeedbackWeights] | None]
    ]
    threshold: int = 60
    basic_threshold: int = 40
    max_age_days: int = 14
    top_k: int = 30
    max_queries: int = 6
    labels: dict[str, str] = field(default_factory=dict)


class SearchService:
    def __init__(self, deps: SearchDeps) -> None:
        self.d = deps
        self._locks: dict[int, asyncio.Lock] = {}

    def is_running(self, user_id: int) -> bool:
        lock = self._locks.get(user_id)
        return bool(lock and lock.locked())

    async def run(
        self, user_id: int, kind: RunType, *, limit: int, progress: Progress | None = None
    ) -> RunOutcome:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            raise SearchInProgressError
        async with lock:
            ctx = await self.d.load_context(user_id)
            if ctx is None:
                raise NotReadyError
            return await self._run(user_id, kind, ctx, limit, progress)

    # ------------------------------------------------------------------ internals
    async def _start_run(self, user_id: int, kind: RunType) -> int:
        async with self.d.sessions.begin() as session:
            run = Run(user_id=user_id, type=kind, started_at=utcnow())
            session.add(run)
            await session.flush()
            return run.id

    async def _finish_run(self, out: RunOutcome) -> None:
        async with self.d.sessions.begin() as session:
            run = await session.get(Run, out.run_id)
            if run is None:
                return
            run.finished_at = utcnow()
            run.counts_json = {
                **out.counts,
                "by_source": out.by_source,
                "llm_scored": out.llm_scored,
                "sent_candidates": len(out.to_send),
            }
            run.errors_json = [{"source": k, "error": v} for k, v in out.errors.items()] + [
                {"source": k, "skipped": v} for k, v in out.skipped.items()
            ]
            run.est_cost_usd = out.cost_usd

    async def _fetch(
        self, source: JobSource, queries: list[SearchQuery], out: RunOutcome
    ) -> list[JobPosting]:
        try:
            jobs = await asyncio.wait_for(source.search(queries), SOURCE_TIMEOUT)
        except QuotaExceededError as exc:
            out.skipped[source.name] = str(exc)
            log.info("source.skipped", source=source.name, reason=str(exc))
            return []
        except Exception as exc:
            out.errors[source.name] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("source.failed", source=source.name, error=str(exc))
            return []
        out.by_source[source.name] = len(jobs)
        return jobs

    async def _persist(self, jobs: list[JobPosting]) -> dict[str, int]:
        """Upsert jobs; returns fingerprint → job id."""
        ids: dict[str, int] = {}
        async with self.d.sessions.begin() as session:
            for job in jobs:
                fp = job.fingerprint
                row = (
                    await session.execute(select(Job).where(Job.fingerprint == fp))
                ).scalar_one_or_none()
                if row is None:
                    row = (
                        await session.execute(
                            select(Job).where(
                                Job.source == job.source, Job.source_job_id == job.source_job_id
                            )
                        )
                    ).scalar_one_or_none()
                values: dict[str, Any] = {
                    "source": job.source,
                    "source_job_id": job.source_job_id[:200],
                    "title": job.title[:300],
                    "company": job.company[:200],
                    "location": (job.location or "")[:300] or None,
                    "remote_type": job.remote_type,
                    "employment_type": job.employment_type,
                    "salary_min": job.salary_min,
                    "salary_max": job.salary_max,
                    "currency": job.currency,
                    "salary_period": job.salary_period,
                    "salary_is_estimated": job.salary_is_estimated,
                    "description": job.description[:20000],
                    "apply_url": job.apply_url[:2000],
                    "posted_at": job.posted_at,
                    "fetched_at": utcnow(),
                    "raw_json": job.raw,
                }
                if row is None:
                    row = Job(fingerprint=fp, **values)
                    session.add(row)
                    await session.flush()
                else:
                    existing_rank = LINK_PRIORITY.get(_kind_of(row), 1)
                    if LINK_PRIORITY[job.link_kind] >= existing_rank:
                        for key, value in values.items():
                            setattr(row, key, value)
                    row.fetched_at = utcnow()
                ids[fp] = row.id
        return ids

    async def _seen(self, user_id: int, job_ids: list[int]) -> set[int]:
        if not job_ids:
            return set()
        async with self.d.sessions() as session:
            rows = await session.execute(
                select(Match.job_id).where(Match.user_id == user_id, Match.job_id.in_(job_ids))
            )
            return set(rows.scalars())

    async def _store_matches(
        self, user_id: int, version: int, items: list[Scored], ids: dict[str, int]
    ) -> None:
        async with self.d.sessions.begin() as session:
            for s in items:
                job_id = ids[s.job.fingerprint]
                existing = (
                    await session.execute(
                        select(Match).where(Match.user_id == user_id, Match.job_id == job_id)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(
                    Match(
                        user_id=user_id,
                        job_id=job_id,
                        profile_version=version,
                        score=s.total,
                        is_basic=s.is_basic,
                        sub_scores_json=s.sub_scores,
                        reasons_json=s.why_fit,
                        missing_skills_json=s.missing_skills,
                        red_flags_json=[*s.red_flags, *(f"⚠ {w}" for w in s.warnings)],
                        pitch=s.pitch,
                        status=MatchStatus.NEW,
                        created_at=utcnow(),
                    )
                )

    async def _run(
        self,
        user_id: int,
        kind: RunType,
        ctx: tuple[int, int, CandidateProfile, Preferences, FeedbackWeights],
        limit: int,
        progress: Progress | None,
    ) -> RunOutcome:
        _pid, version, profile, prefs, weights = ctx
        now = utcnow()
        out = RunOutcome(run_id=await self._start_run(user_id, kind), kind=kind)
        bind_context(run_id=out.run_id, run_kind=kind.value)
        meter = await self.d.cost.meter_for_run()
        token = set_meter(meter)

        async def say(text: str) -> None:
            if progress:
                try:
                    await progress(text)
                except Exception:
                    log.debug("progress.failed")

        try:
            queries, out.used_ai_queries = await build_queries(
                self.d.llm, profile, prefs, limit=self.d.max_queries
            )
            sources = self.d.sources.for_modes(list(prefs.work_modes))
            out.sources_tried = len(sources)
            await say(f"🔎 Searching {len(sources)} sources with {len(queries)} searches…")
            batches = await asyncio.gather(*(self._fetch(s, queries, out) for s in sources))
            found = [j for batch in batches for j in batch]
            unique = dedupe(found)
            out.counts.update(found=len(found), unique=len(unique))
            await say(f"🔎 {len(found)} found → {len(unique)} after removing duplicates…")

            ids = await self._persist(unique)
            seen = await self._seen(user_id, list(ids.values()))
            fresh = [j for j in unique if ids[j.fingerprint] not in seen]
            report = apply_filters(
                fresh, profile, prefs, weights, now=now, max_age_days=self.d.max_age_days
            )
            out.counts.update(
                already_seen=len(unique) - len(fresh), filtered_out=sum(report.dropped.values())
            )
            for reason, n in report.dropped.items():
                out.counts[f"drop_{reason}"] = n

            survivors: list[JobPosting] = []
            warnings: dict[int, list[str]] = {}
            for job in report.kept:
                verdict = assess(job, candidate_level=prefs.experience_level)
                if verdict.drop:
                    out.counts["scam_dropped"] = out.counts.get("scam_dropped", 0) + 1
                    continue
                warnings[id(job)] = verdict.warnings
                survivors.append(job)

            ranked = await stage1(profile, prefs, weights, survivors, self.d.embedder)
            top = ranked[: self.d.top_k]
            for s in top:
                s.warnings = warnings.get(id(s.job), [])
            await say(f"🧠 {len(survivors)} relevant → scoring the best {len(top)}…")
            out.llm_scored = await stage2(self.d.llm, profile, prefs, top, now=now)
            for s in top:
                if not s.sub_scores:
                    apply_basic(s, now)
            strong = [
                s
                for s in rank(top)
                if s.total >= (self.d.basic_threshold if s.is_basic else self.d.threshold)
            ]
            alive, dead = await self.d.links.alive([s.job for s in strong])
            alive_ids = {id(j) for j in alive}
            dropped_dead = {id(s.job) for s in strong if id(s.job) not in alive_ids}
            await self._store_matches(
                user_id, version, [s for s in top if id(s.job) not in dropped_dead], ids
            )
            out.counts.update(scored=len(top), strong=len(strong) - dead, dead_links=dead)
            out.to_send = await self.d.matches.pending(
                user_id,
                threshold=self.d.threshold,
                basic_threshold=self.d.basic_threshold,
                limit=limit,
                max_age_days=self.d.max_age_days,
            )
        finally:
            reset_meter(token)
            out.tokens = meter.usage.total
            out.cost_usd = self.d.cost.cost_of(meter.usage)
            await self._finish_run(out)
        log.info(
            "search.finished",
            **{k: v for k, v in out.counts.items() if isinstance(v, int)},
            errors=len(out.errors),
            tokens=out.tokens,
        )
        return out


def _kind_of(row: Job) -> str:
    return (
        "ats"
        if row.source in {"greenhouse", "lever", "ashby"}
        else ("company" if row.source == "jsearch" else "aggregator")
    )


def top_missing_skills(views: list[MatchView], n: int = 5) -> list[str]:
    counter: Counter[str] = Counter()
    for v in views:
        counter.update(v.missing_skills)
    return [skill for skill, _ in counter.most_common(n)]
