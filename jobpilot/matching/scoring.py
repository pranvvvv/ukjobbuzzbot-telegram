"""Two-stage scoring (spec 8.4).

Stage 1 (cheap, local): embedding similarity + evidence-weighted skill overlap + title
fit + feedback weights. Keeps the top K.
Stage 2 (LLM rubric, batched): sub-scores per rubric, reasons, gaps, pitch. Code clamps
each sub-score and computes the total and freshness (ADR-009); the LLM never does sums.
If the LLM is down or the budget is spent, stage-1 scores become "basic match" scores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel
from rapidfuzz import fuzz

from jobpilot.core.logging import get_logger
from jobpilot.llm.base import Embedder, LLMClient, LLMError, LLMUnavailableError, dumps_compact
from jobpilot.llm.prompting import fence, load_prompt
from jobpilot.matching.normalize import JobPosting, norm_title
from jobpilot.matching.skills import find_skills
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.preferences import FeedbackWeights, Preferences

log = get_logger(__name__)

RUBRIC_MAX = {
    "skills_match": 40,
    "role_fit": 20,
    "seniority_fit": 15,
    "location_mode_fit": 10,
    "preference_fit": 10,
}
FRESHNESS_MAX = 5
BATCH_SIZE = 5


@dataclass
class Scored:
    job: JobPosting
    stage1: float
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    total: int = 0
    sub_scores: dict[str, int] = field(default_factory=dict)
    why_fit: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    pitch: str | None = None
    is_basic: bool = True
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ stage 1
def freshness_points(posted_at: datetime | None, now: datetime) -> int:
    if posted_at is None:
        return 2
    days = (now - posted_at).days
    return 5 if days <= 3 else 4 if days <= 7 else 2 if days <= 14 else 1


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def skill_overlap(profile: CandidateProfile, job: JobPosting) -> tuple[float, list[str], list[str]]:
    """(score 0..1, matched, missing). Evidence-backed skills count fully, listed-only 0.7."""
    job_skills = [
        s for s in find_skills(job.text_for_matching(4000)) if s not in profile.skills.soft
    ]
    if not job_skills:
        return 0.5, [], []
    have = {s.lower() for s in profile.skills.all()}
    matched = [s for s in job_skills if s.lower() in have]
    missing = [s for s in job_skills if s.lower() not in have]
    weight = sum(1.0 if profile.evidence_for(s) else 0.7 for s in matched)
    return min(1.0, weight / len(job_skills)), matched, missing


def title_fit(title: str, roles: list[str]) -> float:
    if not roles:
        return 0.5
    t = norm_title(title)
    return max(fuzz.token_set_ratio(t, norm_title(r)) for r in roles) / 100


def feedback_adjustment(job: JobPosting, weights: FeedbackWeights) -> float:
    title = norm_title(job.title)
    tokens = set(title.split())
    boost = sum(w for term, w in weights.boost_terms.items() if term in tokens or term in title)
    penalty = sum(w for term, w in weights.penalty_terms.items() if term in tokens or term in title)
    return max(-0.3, min(0.15, boost - penalty))


async def stage1(
    profile: CandidateProfile,
    prefs: Preferences,
    weights: FeedbackWeights,
    jobs: list[JobPosting],
    embedder: Embedder,
) -> list[Scored]:
    if not jobs:
        return []
    vectors = await embedder.embed(
        [profile.summary_for_matching()] + [j.text_for_matching(1500) for j in jobs]
    )
    pvec, jvecs = vectors[0], vectors[1:]
    roles = prefs.target_roles or profile.inferred_target_roles
    out = []
    for job, jvec in zip(jobs, jvecs, strict=True):
        cos = max(0.0, min(1.0, (cosine(pvec, jvec) - 0.2) / 0.6))
        skills, matched, missing = skill_overlap(profile, job)
        s1 = 0.45 * cos + 0.35 * skills + 0.20 * title_fit(job.title, roles)
        s1 = max(0.0, min(1.0, s1 + feedback_adjustment(job, weights)))
        out.append(Scored(job, round(s1, 4), matched, missing[:5]))
    out.sort(key=lambda s: s.stage1, reverse=True)
    return out


def basic_total(s: Scored, now: datetime) -> int:
    return min(100, round(s.stage1 * 95) + freshness_points(s.job.posted_at, now))


# ------------------------------------------------------------------ stage 2
class JobScore(BaseModel):
    job_id: str
    skills_match: int
    role_fit: int
    seniority_fit: int
    location_mode_fit: int
    preference_fit: int
    why_fit: list[str]
    missing_skills: list[str]
    red_flags: list[str]
    one_line_pitch: str


class JobScores(BaseModel):
    scores: list[JobScore]


def _job_block(job_id: str, job: JobPosting, now: datetime) -> str:
    age = f"{(now - job.posted_at).days} days ago" if job.posted_at else "unknown"
    salary = (
        f"{job.salary_min or '?'}-{job.salary_max or '?'} {job.currency or ''} "
        f"per {job.salary_period}" + (" (estimate)" if job.salary_is_estimated else "")
        if job.salary_min or job.salary_max
        else "not stated"
    )
    body = (
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'unknown'}"
        f"\nWork mode: {job.remote_type}\nType: {job.employment_type}\nSalary: {salary}"
        f"\nPosted: {age}\n\n{job.description[:2500]}"
    )
    return fence("job", body).replace("<job>", f'<job id="{job_id}">', 1)


def candidate_block(profile: CandidateProfile, prefs: Preferences) -> str:
    data = {
        "summary": profile.summary_for_matching(),
        "skill_evidence": [e.model_dump() for e in profile.skill_evidence[:15]],
        "preferences": prefs.model_dump(
            include={
                "target_roles",
                "job_types",
                "work_modes",
                "locations",
                "experience_level",
                "company_size",
                "must_have_keywords",
                "excluded_keywords",
            }
        )
        | {"min_salary": prefs.min_salary.raw if prefs.min_salary else None},
    }
    return fence("candidate", dumps_compact(data))


def apply_llm_score(s: Scored, score: JobScore, now: datetime) -> None:
    subs = {k: max(0, min(cap, int(getattr(score, k)))) for k, cap in RUBRIC_MAX.items()}
    subs["freshness"] = freshness_points(s.job.posted_at, now)
    s.sub_scores = subs
    s.total = min(100, sum(subs.values()))
    s.why_fit = [w.strip()[:160] for w in score.why_fit if w.strip()][:3]
    s.missing_skills = [m.strip() for m in score.missing_skills if m.strip()][
        :5
    ] or s.missing_skills
    s.red_flags = [r.strip()[:120] for r in score.red_flags if r.strip()][:3]
    s.pitch = score.one_line_pitch.strip()[:200] or None
    s.is_basic = False


def apply_basic(s: Scored, now: datetime) -> None:
    s.total = basic_total(s, now)
    s.sub_scores = {
        "stage1": round(s.stage1 * 100),
        "freshness": freshness_points(s.job.posted_at, now),
    }
    if s.matched_skills:
        s.why_fit = [f"Matches your skills: {', '.join(s.matched_skills[:5])}"]
    s.is_basic = True


async def stage2(
    llm: LLMClient,
    profile: CandidateProfile,
    prefs: Preferences,
    candidates: list[Scored],
    *,
    now: datetime,
) -> int:
    """Score `candidates` in place. Returns how many got an LLM score."""
    if not candidates:
        return 0
    template = load_prompt("score_jobs")
    cand = candidate_block(profile, prefs)
    scored = 0
    llm_down = False
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        if llm_down:
            for s in batch:
                apply_basic(s, now)
            continue
        ids = {f"j{start + i}": s for i, s in enumerate(batch)}
        prompt = template.render(
            candidate=cand,
            jobs="\n\n".join(_job_block(jid, s.job, now) for jid, s in ids.items()),
        )
        try:
            result = await llm.complete_json(JobScores, prompt, max_output_tokens=2500)
        except LLMUnavailableError as exc:
            log.warning("scoring.llm_unavailable", error=str(exc))
            llm_down = True
            for s in batch:
                apply_basic(s, now)
            continue
        except LLMError as exc:  # bad JSON even after repair: skip this batch, keep going
            log.warning("scoring.batch_failed", error=str(exc))
            for s in batch:
                apply_basic(s, now)
            continue
        got = {sc.job_id: sc for sc in result.value.scores if sc.job_id in ids}
        for jid, s in ids.items():
            if jid in got:
                apply_llm_score(s, got[jid], now)
                scored += 1
            else:
                apply_basic(s, now)
    return scored


def rank(items: list[Scored]) -> list[Scored]:
    def key(s: Scored) -> tuple[int, float]:
        return s.total, (s.job.posted_at.timestamp() if s.job.posted_at else 0.0)

    return sorted(items, key=key, reverse=True)
