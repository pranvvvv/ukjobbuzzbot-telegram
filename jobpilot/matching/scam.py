"""Scam and low-quality detection (spec 8.6). Drop clear scams; badge borderline ones."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobpilot.matching.normalize import JobPosting, annualize

_PAYMENT = re.compile(
    r"\b(registration fee|security deposit|training fee|processing fee|pay (?:a |the )?fee|"
    r"refundable (?:fee|deposit)|deposit (?:of )?(?:rs\.?|inr|₹)|kit charges?|joining fee)\b",
    re.I,
)
_CHAT_ONLY = re.compile(
    r"\b(whatsapp|telegram)\b[^.\n]{0,40}\b(only|directly|to apply|for details)\b", re.I
)
_FREE_EMAIL = re.compile(r"[\w.+-]+@(gmail|yahoo|hotmail|outlook|rediffmail)\.com", re.I)
_TOO_GOOD = re.compile(
    r"\b(earn|make)\s+(?:₹|rs\.?|inr|\$)?\s*[\d,]+\s*(?:k|lakh)?\s*(?:per|a|/)\s*"
    r"(?:day|week)\b|\bno experience\b[^.\n]{0,40}\bhigh (?:salary|income)\b|"
    r"\bwork from home\b[^.\n]{0,30}\bearn\b",
    re.I,
)


@dataclass
class ScamVerdict:
    drop: bool = False
    warnings: list[str] = field(default_factory=list)


def assess(job: JobPosting, *, candidate_level: str) -> ScamVerdict:
    v = ScamVerdict()
    text = f"{job.title}\n{job.description}"
    if _PAYMENT.search(text):
        return ScamVerdict(True, ["asks for a fee or deposit"])
    if not job.company or job.company.lower() in {"unknown company", "confidential", "n/a", ""}:
        if job.link_kind == "aggregator":
            return ScamVerdict(True, ["no company name"])
        v.warnings.append("company not named")
    if _CHAT_ONLY.search(text):
        if job.link_kind == "ats":
            v.warnings.append("asks to contact via WhatsApp/Telegram")
        else:
            return ScamVerdict(True, ["WhatsApp/Telegram-only contact"])
    if _TOO_GOOD.search(text):
        v.warnings.append("too-good-to-be-true pay claims")
    if _FREE_EMAIL.search(job.description) and job.link_kind != "ats":
        v.warnings.append("recruiter uses a personal email address")
    if len(job.description.strip()) < 200 and job.source not in {"adzuna"}:
        v.warnings.append("very short description")
    yearly = annualize(job.salary_max, job.salary_period)
    if (
        yearly
        and (job.currency or "").upper() == "INR"
        and not job.salary_is_estimated
        and candidate_level in {"intern", "fresher"}
        and yearly > 5_000_000
    ):
        v.warnings.append("unusually high pay for the level")
    return v
