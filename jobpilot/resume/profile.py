"""Profile post-processing (ADR-009) and the no-AI fallback parser.

`finalize()` turns an extraction (from the LLM or the rule-based parser) into a
`CandidateProfile`: experience computed from dates with overlaps merged, seniority
from rules, skills canonicalised and dropped unless evidenced in the resume text.
"""

from __future__ import annotations

import re
from datetime import date

from jobpilot.core.logging import get_logger
from jobpilot.matching.locations import find_city
from jobpilot.matching.skills import all_skills, find_skills, normalize_skill, skill_in_text
from jobpilot.resume.schema import (
    CandidateProfile,
    Education,
    ExperienceItem,
    Links,
    ProfileExtraction,
    Seniority,
    Skills,
    empty_skills,
)

log = get_logger(__name__)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_SKILL_GROUPS = ("languages", "frameworks", "tools", "cloud", "databases", "ml_ai", "soft")


# ------------------------------------------------------------------ dates
def parse_month(value: str | None, today: date) -> tuple[int, int] | None:
    """'2024-06' / '2024' / 'present' → (year, month)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in {"present", "current", "now", "ongoing", "till date", "today"}:
        return today.year, today.month
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?", v)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2) or 1)
    if not (1950 <= year <= today.year + 6 and 1 <= month <= 12):
        return None
    return year, month


def _months(ym: tuple[int, int]) -> int:
    return ym[0] * 12 + ym[1] - 1


def merged_months(items: list[ExperienceItem], today: date, *, internships: bool) -> int:
    """Total months covered by roles of one kind, with overlapping periods merged."""
    spans: list[tuple[int, int]] = []
    now = today.year * 12 + today.month - 1
    for item in items:
        if item.is_internship != internships:
            continue
        start = parse_month(item.start, today)
        if start is None:
            continue
        end = parse_month(item.end, today) or (today.year, today.month)
        a, b = _months(start), min(_months(end), now)
        if b >= a:
            spans.append((a, b + 1))  # inclusive month count
    spans.sort()
    total, cur = 0, None
    for a, b in spans:
        if cur is None or a > cur[1]:
            if cur:
                total += cur[1] - cur[0]
            cur = (a, b)
        else:
            cur = (cur[0], max(cur[1], b))
    if cur:
        total += cur[1] - cur[0]
    return total


def infer_seniority(
    years: float, is_student: bool, internship_months: int, title: str | None
) -> Seniority:
    t = (title or "").lower()
    if years < 0.5:
        return "intern" if is_student else "fresher"
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years >= 8 or re.search(r"\b(lead|principal|staff|head|architect|manager)\b", t):
        return "lead" if years >= 6 else "senior"
    return "senior"


# ------------------------------------------------------------------ finalize
def _canonical_skills(extraction: ProfileExtraction, evidence_text: str) -> tuple[Skills, int]:
    vocab = all_skills()
    grouped: dict[str, list[str]] = {g: [] for g in _SKILL_GROUPS}
    dropped = 0
    for group in _SKILL_GROUPS:
        for raw in getattr(extraction.skills, group):
            name = normalize_skill(raw)
            if not name or (
                not skill_in_text(raw, evidence_text) and not skill_in_text(name, evidence_text)
            ):
                dropped += 1
                continue
            target = vocab.get(name, group)
            if name not in grouped[target]:
                grouped[target].append(name)
    return Skills(**grouped), dropped


def finalize(
    extraction: ProfileExtraction,
    resume_text: str,
    *,
    today: date,
    extra_evidence: str = "",
    parsed_with_ai: bool = True,
) -> CandidateProfile:
    evidence_text = f"{resume_text}\n{extra_evidence}"
    skills, dropped = _canonical_skills(extraction, evidence_text)
    if dropped:
        log.info("profile.skills_dropped_without_evidence", count=dropped)
    kept = {s.lower() for s in skills.all()}
    evidence = [
        ev.model_copy(update={"skill": normalize_skill(ev.skill)})
        for ev in extraction.skill_evidence
        if normalize_skill(ev.skill).lower() in kept
    ]
    years_months = merged_months(extraction.experience, today, internships=False)
    intern_months = merged_months(extraction.experience, today, internships=True)
    years = round(years_months / 12, 1)
    ongoing = any(e.status == "ongoing" for e in extraction.education) or any(
        e.grad_year and e.grad_year > today.year for e in extraction.education
    )
    is_student = extraction.is_student or ongoing
    roles: list[str] = []
    for role in extraction.inferred_target_roles:
        clean = role.strip()
        if clean and clean.lower() not in {r.lower() for r in roles}:
            roles.append(clean)
    if not roles:
        roles = roles_from_skills(skills.all())
    flags = list(dict.fromkeys(extraction.gaps_or_flags))
    if not extraction.location_current:
        flags.append("no location found")
    return CandidateProfile(
        **extraction.model_dump(
            exclude={
                "skills",
                "skill_evidence",
                "inferred_target_roles",
                "gaps_or_flags",
                "is_student",
            }
        ),
        skills=skills,
        skill_evidence=evidence,
        inferred_target_roles=roles[:6],
        gaps_or_flags=list(dict.fromkeys(flags)),
        is_student=is_student,
        total_experience_years=years,
        internship_months=intern_months,
        seniority=infer_seniority(
            years, is_student, intern_months, extraction.current_or_last_title
        ),
        parsed_with_ai=parsed_with_ai,
    )


# ------------------------------------------------------------------ roles from skills
_ROLE_RULES: list[tuple[str, set[str]]] = [
    ("AI Engineer", {"LLMs", "RAG", "LangChain", "OpenAI API", "AI Agents", "Hugging Face"}),
    (
        "Machine Learning Engineer",
        {"Machine Learning", "Deep Learning", "PyTorch", "TensorFlow", "scikit-learn"},
    ),
    ("AI Automation Engineer", {"n8n", "Zapier", "Make.com", "Chatbots", "WhatsApp Business API"}),
    (
        "Backend Developer",
        {
            "FastAPI",
            "Django",
            "Flask",
            "Node.js",
            "Express.js",
            "Spring Boot",
            "NestJS",
            "PostgreSQL",
            "REST APIs",
        },
    ),
    ("Frontend Developer", {"React", "Next.js", "Vue.js", "Angular", "Tailwind CSS"}),
    ("Full Stack Developer", set()),  # added when both backend and frontend match
    ("Data Analyst", {"Data Analysis", "Tableau", "Power BI", "Excel", "Pandas"}),
    ("Data Engineer", {"PySpark", "Airflow", "dbt", "Snowflake", "BigQuery", "Kafka"}),
    ("Mobile App Developer", {"Flutter", "React Native", "Kotlin", "Swift"}),
    ("DevOps Engineer", {"Kubernetes", "Terraform", "Ansible", "Jenkins", "CI/CD"}),
]


def roles_from_skills(skills: list[str]) -> list[str]:
    have = set(skills)
    scored = [(len(have & req), role) for role, req in _ROLE_RULES if req and have & req]
    scored.sort(key=lambda x: -x[0])
    roles = [r for _, r in scored]
    if "Backend Developer" in roles and "Frontend Developer" in roles:
        roles.insert(0, "Full Stack Developer")
    return roles[:5] or ["Software Engineer"]


# ------------------------------------------------------------------ rule-based parser
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?\d{1,3}[\s-]?)?\d{5}[\s-]?\d{5}|\+?\d[\d\s-]{9,14}\d")
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+", re.I)
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w-]+/?", re.I)
_URL = re.compile(
    r"(?:https?://)?(?:www\.)?[\w-]+\.(?:com|in|io|dev|me|app|co|tech)(?:/[\w./-]*)?", re.I
)
_MONTH_WORD = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH_WORD}\s*'?\d{{2,4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to)\s*(?P<end>{_DATE}|present|current|now|till date)", re.I
)
_DEGREE = re.compile(
    r"\b(b\.?\s?tech|b\.?\s?e\.?|b\.?\s?sc|bca|mca|m\.?\s?tech|m\.?\s?sc|mba|bba|b\.?com|"
    r"ph\.?d|bachelor[’']?s?|master[’']?s?|diploma)\b",
    re.I,
)


def _to_ym(token: str, today: date) -> str | None:
    t = token.strip().lower()
    if t in {"present", "current", "now", "till date"}:
        return "present"
    m = re.match(rf"{_MONTH_WORD}\s*'?(\d{{2,4}})", t)
    if m:
        year = int(m.group(2))
        year = year + 2000 if year < 100 else year
        return f"{year:04d}-{_MONTHS[m.group(1)[:3]]:02d}"
    m = re.match(r"(\d{1,2})/(\d{4})", t)
    if m:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})", t)
    return f"{m.group(1)}-01" if m else None


def heuristic_extract(text: str, today: date) -> ProfileExtraction:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = next(
        (ln for ln in lines[:5] if not re.search(r"[\d@/:|]", ln) and 1 <= len(ln.split()) <= 5),
        None,
    )
    email = _EMAIL.search(text)
    phone = _PHONE.search(text)
    github = _GITHUB.search(text)
    linkedin = _LINKEDIN.search(text)
    portfolio = next(
        (
            u.group(0)
            for u in _URL.finditer(text)
            if "github" not in u.group(0).lower()
            and "linkedin" not in u.group(0).lower()
            and "@" not in text[max(0, u.start() - 1) : u.start()]
        ),
        None,
    )
    found = find_skills(text)
    vocab = all_skills()
    groups: dict[str, list[str]] = {g: [] for g in _SKILL_GROUPS}
    for skill in found:
        groups[vocab.get(skill, "tools")].append(skill)

    experience: list[ExperienceItem] = []
    for i, line in enumerate(lines):
        m = _RANGE.search(line)
        if not m:
            continue
        if _DEGREE.search(line) or re.search(
            r"\b(university|college|school|institute)\b", line, re.I
        ):
            continue
        title_text = line[: m.start()].strip(" |,-–—") or (lines[i - 1] if i else "Role")
        context = " ".join(lines[max(0, i - 1) : i + 2]).lower()
        parts = re.split(r"\s+(?:at|@|-|–|—|\|)\s+|,\s*", title_text, maxsplit=1)
        experience.append(
            ExperienceItem(
                title=parts[0][:80],
                company=(parts[1] if len(parts) > 1 else "")[:80] or "Unknown",
                start=_to_ym(m.group("start"), today),
                end=_to_ym(m.group("end"), today),
                is_internship="intern" in context or "trainee" in context,
                highlights=[],
            )
        )

    education: list[Education] = []
    for i, line in enumerate(lines):
        if not _DEGREE.search(line):
            continue
        years = [
            int(y) for y in re.findall(r"\b(19[89]\d|20[0-4]\d)\b", " ".join(lines[i : i + 2]))
        ]
        grad = max(years) if years else None
        ongoing = bool(re.search(r"pursuing|expected|ongoing|present|current", line, re.I)) or (
            grad is not None and grad > today.year
        )
        education.append(
            Education(
                degree=_DEGREE.search(line).group(0),  # type: ignore[union-attr]
                field=None,
                institution=(lines[i + 1] if i + 1 < len(lines) else "")[:100],
                grad_year=grad,
                status="ongoing" if ongoing else ("complete" if grad else "unknown"),
            )
        )
        if len(education) >= 3:
            break

    return ProfileExtraction(
        full_name=name,
        email=email.group(0) if email else None,
        phone=phone.group(0) if phone else None,
        location_current=find_city("\n".join(lines[:15])) or find_city(text),
        is_student=any(e.status == "ongoing" for e in education),
        current_or_last_title=experience[0].title if experience else None,
        education=education,
        skills=Skills(**groups) if found else empty_skills(),
        skill_evidence=[],
        domains=[],
        projects=[],
        experience=experience[:8],
        certifications=[],
        achievements=[],
        inferred_target_roles=roles_from_skills(found),
        links=Links(
            github=github.group(0) if github else None,
            portfolio=portfolio,
            linkedin=linkedin.group(0) if linkedin else None,
        ),
        gaps_or_flags=["parsed without AI: please check the details"],
    )


RESUME_SIGNALS = re.compile(
    r"\b(education|experience|skills|projects?|internship|university|college|b\.?tech|"
    r"bachelor|resume|curriculum vitae|cv|certifications?|objective|summary|work history)\b",
    re.I,
)


def looks_like_resume(text: str) -> bool:
    """Cheap no-AI check used when the classifier is unavailable."""
    hits = {m.group(0).lower() for m in RESUME_SIGNALS.finditer(text)}
    return len(hits) >= 3 or (len(hits) >= 2 and bool(_EMAIL.search(text)))
