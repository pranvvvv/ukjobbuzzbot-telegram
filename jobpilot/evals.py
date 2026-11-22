"""Matching-quality eval (spec 13): labeled resume↔job pairs → precision@k and agreement.

Runs the real pipeline pieces (hard filters → stage 1 → optional stage 2) on a fixed,
labeled dataset, so changes to scoring can be measured instead of guessed.

Metrics per persona, then averaged:
- precision@5 / precision@10: share of "good" jobs among the top k by score.
- pairwise accuracy: of all (good, bad) pairs, how often the good job scores higher.
- Spearman ρ between score and label (bad=0, ok=1, good=2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from jobpilot.core.timeutil import utcnow
from jobpilot.llm.base import Embedder, LLMClient
from jobpilot.matching.filters import drop_reason
from jobpilot.matching.locations import find_city, find_country
from jobpilot.matching.normalize import JobPosting
from jobpilot.matching.scoring import apply_basic, stage1, stage2
from jobpilot.resume.schema import CandidateProfile
from jobpilot.services.preferences import FeedbackWeights, Preferences

DATASET = Path(__file__).resolve().parents[1] / "evals" / "dataset.yaml"
LABEL_VALUE = {"bad": 0, "ok": 1, "good": 2}


@dataclass
class PersonaResult:
    persona: str
    scores: dict[str, int]
    labels: dict[str, str]
    dropped: dict[str, str] = field(default_factory=dict)

    def ranked(self) -> list[str]:
        return sorted(self.scores, key=lambda j: (-self.scores[j], j))

    def precision_at(self, k: int) -> float:
        top = self.ranked()[:k]
        return sum(self.labels[j] == "good" for j in top) / max(1, len(top))

    def pairwise_accuracy(self) -> float:
        good = [j for j, lbl in self.labels.items() if lbl == "good"]
        bad = [j for j, lbl in self.labels.items() if lbl == "bad"]
        pairs = [(g, b) for g in good for b in bad]
        if not pairs:
            return 1.0
        wins = sum(
            1.0
            if self.scores[g] > self.scores[b]
            else 0.5
            if self.scores[g] == self.scores[b]
            else 0.0
            for g, b in pairs
        )
        return wins / len(pairs)

    def spearman(self) -> float:
        ids = list(self.labels)
        return _spearman([self.scores[i] for i in ids], [LABEL_VALUE[self.labels[i]] for i in ids])


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _ranks(a), _ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def _job(raw: dict[str, Any]) -> JobPosting:
    now = utcnow()
    return JobPosting(
        source="eval",
        source_job_id=raw["id"],
        title=raw["title"],
        company=raw["company"],
        location=raw["location"],
        city=find_city(raw["location"]),
        country=find_country(raw["location"]),
        remote_type=raw["remote_type"],
        employment_type=raw["employment_type"],
        description=raw["description"],
        apply_url=f"https://example.com/{raw['id']}",
        link_kind="ats",
        posted_at=now - timedelta(days=int(raw.get("days_old", 3))),
    )


def _profile(raw: dict[str, Any]) -> CandidateProfile:
    base: dict[str, Any] = {
        "full_name": None,
        "email": None,
        "phone": None,
        "location_current": None,
        "is_student": False,
        "current_or_last_title": None,
        "education": [],
        "skill_evidence": [],
        "domains": [],
        "projects": [],
        "experience": [],
        "certifications": [],
        "achievements": [],
        "inferred_target_roles": [],
        "links": {"github": None, "portfolio": None, "linkedin": None},
        "gaps_or_flags": [],
    }
    return CandidateProfile.model_validate(base | raw)


def load_dataset(path: Path = DATASET) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


async def run_eval(
    embedder: Embedder, llm: LLMClient | None = None, data: dict[str, Any] | None = None
) -> list[PersonaResult]:
    data = data or load_dataset()
    jobs = {raw["id"]: _job(raw) for raw in data["jobs"]}
    now = utcnow()
    results = []
    for persona in data["personas"]:
        profile = _profile(persona["profile"])
        prefs = Preferences.model_validate(persona["prefs"])
        weights = FeedbackWeights()
        result = PersonaResult(persona["id"], {}, dict(persona["labels"]))
        kept: list[JobPosting] = []
        for jid, job in jobs.items():
            reason = drop_reason(job, profile, prefs, weights, now=now, max_age_days=14)
            if reason:
                result.scores[jid], result.dropped[jid] = 0, reason
            else:
                kept.append(job)
        scored = await stage1(profile, prefs, weights, kept, embedder)
        if llm is not None:
            await stage2(llm, profile, prefs, scored, now=now)
        for s in scored:
            if not s.sub_scores:
                apply_basic(s, now)
            result.scores[s.job.source_job_id] = s.total
        results.append(result)
    return results


def report_markdown(results: list[PersonaResult], *, embedder_name: str, llm_name: str) -> str:
    def avg(fn: Callable[[PersonaResult], float]) -> float:
        return float(sum(fn(r) for r in results)) / len(results)

    lines = [
        "# Matching eval results",
        "",
        f"Dataset: {sum(len(r.labels) for r in results)} labeled pairs "
        f"({len(results)} personas). Embedder: `{embedder_name}`. Stage 2: `{llm_name}`.",
        "",
        "| Persona | P@5 | P@10 | Pairwise acc. | Spearman ρ | Hard-filtered |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.persona} | {r.precision_at(5):.2f} | {r.precision_at(10):.2f} | "
            f"{r.pairwise_accuracy():.2f} | {r.spearman():.2f} | {len(r.dropped)} |"
        )
    lines.append(
        f"| **mean** | **{avg(lambda r: r.precision_at(5)):.2f}** | "
        f"**{avg(lambda r: r.precision_at(10)):.2f}** | "
        f"**{avg(PersonaResult.pairwise_accuracy):.2f}** | "
        f"**{avg(PersonaResult.spearman):.2f}** | |"
    )
    lines += ["", "P@10 is capped by how many jobs are labeled *good* per persona (4–5 of 15).", ""]
    for r in results:
        lines += [
            f"## {r.persona}",
            "",
            "| Rank | Job | Score | Label | Note |",
            "|---|---|---|---|---|",
        ]
        for i, jid in enumerate(r.ranked(), 1):
            lines.append(
                f"| {i} | {jid} | {r.scores[jid]} | {r.labels[jid]} | "
                f"{'filtered: ' + r.dropped[jid] if jid in r.dropped else ''} |"
            )
        lines.append("")
    return "\n".join(lines)
