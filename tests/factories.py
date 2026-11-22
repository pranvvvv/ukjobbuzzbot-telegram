"""Builders for synthetic test data (never real people)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jobpilot.matching.normalize import JobPosting
from jobpilot.resume.schema import (
    CandidateProfile,
    Education,
    ExperienceItem,
    Links,
    Project,
    SkillEvidence,
    Skills,
)
from jobpilot.services.preferences import Preferences

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def profile(**overrides: Any) -> CandidateProfile:
    data: dict[str, Any] = {
        "full_name": "Aarav Mehta",
        "email": "aarav.mehta@example.com",
        "phone": None,
        "location_current": "Hyderabad",
        "is_student": True,
        "current_or_last_title": "Backend Intern",
        "education": [
            Education(
                degree="B.Tech", field="CSE", institution="JNTU", grad_year=2027, status="ongoing"
            )
        ],
        "skills": Skills(
            languages=["Python", "JavaScript", "SQL"],
            frameworks=["FastAPI", "Django", "React"],
            tools=["Git", "Docker"],
            cloud=[],
            databases=["PostgreSQL", "Redis"],
            ml_ai=["LangChain", "RAG"],
            soft=["Communication"],
        ),
        "skill_evidence": [
            SkillEvidence(skill="FastAPI", where="job: Pixel Labs"),
            SkillEvidence(skill="Python", where="project: WhatsApp bot"),
        ],
        "domains": ["AI automation"],
        "projects": [
            Project(
                name="WhatsApp Support Bot", summary="Automated support", tech=["Python", "FastAPI"]
            )
        ],
        "experience": [
            ExperienceItem(
                title="Backend Intern",
                company="Pixel Labs",
                start="2025-06",
                end="2025-08",
                is_internship=True,
                highlights=["Built REST APIs"],
            )
        ],
        "certifications": [],
        "achievements": [],
        "inferred_target_roles": ["Backend Developer", "AI Engineer", "Full Stack Developer"],
        "links": Links(github=None, portfolio=None, linkedin=None),
        "gaps_or_flags": [],
        "total_experience_years": 0.0,
        "internship_months": 3,
        "seniority": "intern",
    }
    data.update(overrides)
    return CandidateProfile(**data)


def prefs(**overrides: Any) -> Preferences:
    data: dict[str, Any] = {
        "target_roles": ["Backend Developer", "AI Engineer"],
        "job_types": ["full_time", "internship"],
        "work_modes": ["remote", "hybrid", "onsite"],
        "locations": ["Hyderabad"],
        "willing_to_relocate": False,
        "experience_level": "intern",
    }
    data.update(overrides)
    return Preferences(**data)


_counter = iter(range(1, 100_000))


def job(**overrides: Any) -> JobPosting:
    n = next(_counter)
    data: dict[str, Any] = {
        "source": "greenhouse",
        "source_job_id": f"acme:{n}",
        "title": "Backend Engineer Intern (Python)",
        "company": "Acme",
        "location": "Hyderabad, India",
        "city": "Hyderabad",
        "country": "India",
        "remote_type": "hybrid",
        "employment_type": "internship",
        "description": "Build FastAPI services with PostgreSQL and Docker. " * 10,
        "apply_url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "link_kind": "ats",
        "posted_at": NOW - timedelta(days=2),
    }
    data.update(overrides)
    return JobPosting(**data)
