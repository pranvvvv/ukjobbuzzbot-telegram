"""Candidate profile schemas (spec 4.3).

`ProfileExtraction` is what the LLM returns: strict-mode compatible (every field
required, optional written as `X | None`, no extra keys). `CandidateProfile` adds the
fields our code computes (ADR-009): experience years, internship months, seniority.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Seniority = Literal["intern", "fresher", "junior", "mid", "senior", "lead"]
SENIORITY_ORDER: tuple[Seniority, ...] = ("intern", "fresher", "junior", "mid", "senior", "lead")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Education(Strict):
    degree: str
    field: str | None
    institution: str
    grad_year: int | None
    status: Literal["ongoing", "complete", "unknown"]


class Skills(Strict):
    languages: list[str]
    frameworks: list[str]
    tools: list[str]
    cloud: list[str]
    databases: list[str]
    ml_ai: list[str]
    soft: list[str]

    def all(self) -> list[str]:
        out: list[str] = []
        for group in (
            self.languages,
            self.frameworks,
            self.ml_ai,
            self.databases,
            self.cloud,
            self.tools,
            self.soft,
        ):
            out.extend(s for s in group if s not in out)
        return out


class SkillEvidence(Strict):
    skill: str
    where: str


class Project(Strict):
    name: str
    summary: str
    tech: list[str]


class ExperienceItem(Strict):
    title: str
    company: str
    start: str | None  # "YYYY-MM"
    end: str | None  # "YYYY-MM" | "present"
    is_internship: bool
    highlights: list[str]


class Links(Strict):
    github: str | None
    portfolio: str | None
    linkedin: str | None


class ResumeCheck(Strict):
    is_resume: bool
    reason: str


class ProfileExtraction(Strict):
    full_name: str | None
    email: str | None
    phone: str | None
    location_current: str | None
    is_student: bool
    current_or_last_title: str | None
    education: list[Education]
    skills: Skills
    skill_evidence: list[SkillEvidence]
    domains: list[str]
    projects: list[Project]
    experience: list[ExperienceItem]
    certifications: list[str]
    achievements: list[str]
    inferred_target_roles: list[str]
    links: Links
    gaps_or_flags: list[str]


class CandidateProfile(ProfileExtraction):
    """Stored profile: extraction + code-computed fields."""

    model_config = ConfigDict(extra="ignore")

    total_experience_years: float = 0.0
    internship_months: int = 0
    seniority: Seniority = "fresher"
    parsed_with_ai: bool = True

    def evidence_for(self, skill: str) -> str | None:
        for ev in self.skill_evidence:
            if ev.skill.lower() == skill.lower():
                return ev.where
        return None

    def top_skills(self, n: int = 10) -> list[str]:
        """Evidence-backed skills first, then the rest; soft skills last."""
        all_skills = self.skills.all()
        backed = [s for s in all_skills if self.evidence_for(s)]
        rest = [s for s in all_skills if s not in backed and s not in self.skills.soft]
        return (backed + rest)[:n]

    def summary_for_matching(self) -> str:
        """Compact text used for embeddings and LLM scoring context."""
        parts = [
            f"Target roles: {', '.join(self.inferred_target_roles)}",
            f"Level: {self.seniority}, {self.total_experience_years} years experience"
            + (" (student)" if self.is_student else ""),
            f"Skills: {', '.join(self.skills.all())}",
        ]
        if self.domains:
            parts.append(f"Domains: {', '.join(self.domains)}")
        for p in self.projects[:5]:
            parts.append(f"Project {p.name}: {p.summary} ({', '.join(p.tech)})")
        for e in self.experience[:4]:
            parts.append(f"{e.title} at {e.company}: {'; '.join(e.highlights[:2])}")
        return "\n".join(parts)


class ProfileSkillsOnly(BaseModel):
    """Used by tests and the eval set to build compact personas."""

    skills: list[str] = Field(default_factory=list)


def empty_skills() -> Skills:
    return Skills(languages=[], frameworks=[], tools=[], cloud=[], databases=[], ml_ai=[], soft=[])


def empty_links() -> Links:
    return Links(github=None, portfolio=None, linkedin=None)
