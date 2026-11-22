from __future__ import annotations

import pytest

from jobpilot.services.preferences import (
    BY_ID,
    InputError,
    applicable,
    apply_choice,
    apply_text,
    next_question,
    parse_keywords,
    parse_salary,
    parse_time,
    position,
    prefill,
    prev_question,
    skip,
)
from tests import factories


@pytest.mark.parametrize(
    ("text", "amount", "currency", "period"),
    [
        ("6 LPA", 600_000, "INR", "year"),
        ("12.5 lakhs", 1_250_000, "INR", "year"),
        ("25k per month", 25_000, "INR", "month"),
        ("₹8,00,000", 800_000, "INR", "year"),
        ("$60k", 60_000, "USD", "year"),
        ("stipend 15000", 15_000, "INR", "month"),
    ],
)
def test_parse_salary(text: str, amount: float, currency: str, period: str) -> None:
    s = parse_salary(text)
    assert s is not None
    assert (s.amount, s.currency, s.period) == (amount, currency, period)


def test_parse_salary_rejects_nonsense() -> None:
    assert parse_salary("whatever pays") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9", "09:00"),
        ("7pm", "19:00"),
        ("12am", "00:00"),
        ("08:30", "08:30"),
        ("21.15", "21:15"),
        ("25:00", None),
        ("noon", None),
    ],
)
def test_parse_time(text: str, expected: str | None) -> None:
    assert parse_time(text) == expected


def test_parse_keywords_splits_must_haves_and_deal_breakers() -> None:
    must, avoid = parse_keywords("Python only, no sales roles, avoid night shifts, FastAPI")
    assert must == ["Python", "FastAPI"]
    assert avoid == ["sales", "night shifts"]


def test_prefill_uses_resume() -> None:
    p = prefill(factories.profile(), jobs_per_digest=10, digest_time="09:00")
    assert p.target_roles == ["Backend Developer", "AI Engineer", "Full Stack Developer"]
    assert p.locations == ["Hyderabad"]
    assert p.job_types == ["internship"]
    assert p.experience_level == "intern"


def test_only_needed_questions_are_asked() -> None:
    profile = factories.profile()
    prefs = prefill(profile, jobs_per_digest=10, digest_time="09:00")
    ids = [q.id for q in applicable(prefs, profile)]
    assert "level" not in ids  # AI-parsed profile with clear dates: level is inferred
    assert "visa" not in ids  # no foreign locations
    remote_only = apply_choice(BY_ID["work_modes"], ["remote"], prefs)
    assert "locations" not in [q.id for q in applicable(remote_only, profile)]
    abroad = apply_text(BY_ID["locations"], "Hyderabad, Berlin", prefs)
    assert "visa" in [q.id for q in applicable(abroad, profile)]
    unsure = factories.profile(parsed_with_ai=False)
    assert "level" in [q.id for q in applicable(prefs, unsure)]


def test_navigation_and_progress() -> None:
    profile = factories.profile()
    prefs = prefill(profile, jobs_per_digest=10, digest_time="09:00")
    first = next_question(None, prefs, profile)
    assert first is not None and first.id == "roles"
    second = next_question("roles", prefs, profile)
    assert second is not None and second.id == "job_types"
    back = prev_question("job_types", prefs, profile)
    assert back is not None and back.id == "roles"
    assert prev_question("roles", prefs, profile) is None
    idx, total = position("job_types", prefs, profile)
    assert idx == 2 and total == len(applicable(prefs, profile))


def test_text_answers_and_skips() -> None:
    prefs = factories.prefs()
    prefs = apply_text(BY_ID["locations"], "bangalore, Gurgaon", prefs)
    assert prefs.locations == ["Bengaluru", "Delhi NCR"]
    prefs = apply_text(BY_ID["salary"], "6 LPA", prefs)
    assert prefs.min_salary is not None and prefs.min_salary.annual() == 600_000
    prefs = skip(BY_ID["salary"], prefs)
    assert prefs.min_salary is None
    prefs = apply_text(BY_ID["roles"], "Data Engineer, ML Engineer", prefs)
    assert prefs.target_roles[-2:] == ["Data Engineer", "ML Engineer"]
    with pytest.raises(InputError):
        apply_text(BY_ID["salary"], "lots", prefs)
    with pytest.raises(InputError):
        apply_text(BY_ID["job_types"], "full time", prefs)
    prefs = apply_choice(BY_ID["per_digest"], ["15"], prefs)
    assert prefs.jobs_per_digest == 15
