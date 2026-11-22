"""Normalisation helpers shared by every source (spec 8.1) and the normalised job model."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from jobpilot.matching.locations import detect_work_mode, find_city, find_country

RemoteType = Literal["remote", "hybrid", "onsite", "unknown"]
EmploymentType = Literal["full_time", "internship", "contract", "part_time", "freelance", "unknown"]
LinkKind = Literal["ats", "company", "aggregator"]
LINK_PRIORITY: dict[str, int] = {"ats": 3, "company": 2, "aggregator": 1}


class JobPosting(BaseModel):
    """One normalised job from any source."""

    source: str
    source_job_id: str
    title: str
    company: str
    location: str | None = None
    city: str | None = None
    country: str | None = None
    remote_type: RemoteType = "unknown"
    remote_region: str | None = None  # e.g. "USA only", "Worldwide" for remote boards
    employment_type: EmploymentType = "unknown"
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    salary_period: Literal["year", "month", "hour"] | None = None
    salary_is_estimated: bool = False
    description: str = ""
    apply_url: str
    link_kind: LinkKind = "aggregator"
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.company, self.title, self.city or self.remote_type)

    def text_for_matching(self, limit: int = 3000) -> str:
        return f"{self.title}\n{self.company}\n{self.location or ''}\n{self.description[:limit]}"


# ------------------------------------------------------------------ text
def html_to_text(value: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse returns it) → readable plain text."""
    if not value:
        return ""
    if "&lt;" in value and "<" not in value:
        value = html.unescape(value)
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        br.insert_before("\n")
    text = soup.get_text(" ")
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_COMPANY_SUFFIX = re.compile(
    r"\b(private limited|pvt\.? ltd\.?|pvt|ltd\.?|limited|inc\.?|llc|llp|corp\.?|corporation|"
    r"co\.|gmbh|technologies|technology|solutions|labs|india)\b",
    re.I,
)


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def norm_company(name: str) -> str:
    n = _ascii_fold(name).lower()
    n = _COMPANY_SUFFIX.sub(" ", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def norm_title(title: str) -> str:
    t = _ascii_fold(title).lower()
    t = re.sub(r"\((m/f/d|f/m/d|m/w/d|all genders)\)", " ", t)
    t = re.sub(r"\b(sr)\b\.?", "senior", t)
    t = re.sub(r"\b(jr)\b\.?", "junior", t)
    t = re.sub(r"\b(sde)\b", "software development engineer", t)
    return re.sub(r"[^a-z0-9+#]+", " ", t).strip()


def fingerprint(company: str, title: str, place: str | None) -> str:
    key = f"{norm_company(company)}|{norm_title(title)}|{(place or '').lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()


# ------------------------------------------------------------------ fields
def parse_datetime(value: Any) -> datetime | None:
    """ISO strings, epoch seconds or epoch milliseconds → aware UTC datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (ValueError, OSError, OverflowError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


_EMPLOYMENT: dict[str, EmploymentType] = {
    "full_time": "full_time",
    "fulltime": "full_time",
    "full-time": "full_time",
    "permanent": "full_time",
    "full time": "full_time",
    "intern": "internship",
    "internship": "internship",
    "trainee": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "contract",
    "temp": "contract",
    "fixed term": "contract",
    "part_time": "part_time",
    "parttime": "part_time",
    "part-time": "part_time",
    "part time": "part_time",
    "freelance": "freelance",
}


def employment_type(*values: str | None, title: str = "") -> EmploymentType:
    for value in values:
        if value:
            key = re.sub(r"\s+", " ", value.strip().lower())
            if key in _EMPLOYMENT:
                return _EMPLOYMENT[key]
            for k, v in _EMPLOYMENT.items():
                if k in key:
                    return v
    if re.search(r"\b(intern|internship|trainee)\b", title, re.I):
        return "internship"
    if re.search(r"\b(contract|freelance)\b", title, re.I):
        return "contract"
    return "unknown"


def place_fields(location: str | None, *extra: str | None) -> dict[str, Any]:
    """city / country / remote_type derived from a location string (+ title etc.)."""
    mode = detect_work_mode(location, *extra)
    return {
        "city": find_city(location),
        "country": find_country(location),
        "remote_type": mode,
    }


SALARY_PERIODS: dict[str, Literal["year", "month", "hour"]] = {
    "year": "year",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "per_year": "year",
    "month": "month",
    "monthly": "month",
    "per_month": "month",
    "hour": "hour",
    "hourly": "hour",
    "per_hour": "hour",
}


def salary_period(value: str | None) -> Literal["year", "month", "hour"] | None:
    if not value:
        return None
    return SALARY_PERIODS.get(value.strip().lower().replace(" ", "_"))


def annualize(amount: float | None, period: str | None) -> float | None:
    if amount is None:
        return None
    return amount * {"month": 12, "hour": 2080}.get(period or "year", 1)


_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|head|director|architect|manager|vp)\b", re.I
)
_JUNIOR = re.compile(
    r"\b(junior|jr\.?|associate|graduate|entry[- ]level|fresher|new grad|"
    r"trainee|sde[- ]?(i|1)\b|engineer i\b|level 1)\b",
    re.I,
)
_INTERN = re.compile(r"\b(intern|internship|apprentice)\b", re.I)
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)", re.I
)


def title_level(title: str) -> str:
    """intern | junior | senior | lead | unknown, from the job title alone."""
    if _INTERN.search(title):
        return "intern"
    if re.search(r"\b(principal|staff|head|director|vp|architect)\b", title, re.I):
        return "lead"
    if _SENIOR.search(title):
        return "senior"
    if _JUNIOR.search(title):
        return "junior"
    return "unknown"


def required_years(text: str) -> int | None:
    """Minimum years of experience asked for ("3+ years", "2-4 yrs"), sanity-bounded."""
    found = [int(m.group(1)) for m in _YEARS.finditer(text[:6000]) if int(m.group(1)) <= 20]
    return min(found) if found else None
