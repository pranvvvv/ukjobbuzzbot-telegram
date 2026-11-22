from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from jobpilot.bot.delivery import REASONS, JAction, JobCb, card_keyboard, reason_keyboard
from jobpilot.bot.formatting import (
    TELEGRAM_MESSAGE_LIMIT,
    format_salary,
    job_card,
    posted_ago,
    utf16_len,
)
from jobpilot.db.models import MatchStatus
from jobpilot.llm.base import Usage, UsageMeter, reset_meter, set_meter
from jobpilot.llm.fake import FakeLLM, HashEmbedder, OfflineLLM
from jobpilot.matching.dedupe import dedupe
from jobpilot.matching.filters import apply_filters, drop_reason
from jobpilot.matching.links import LinkChecker
from jobpilot.matching.locations import detect_work_mode, find_city, remote_allows_country
from jobpilot.matching.normalize import (
    employment_type,
    html_to_text,
    norm_company,
    parse_datetime,
    required_years,
    title_level,
)
from jobpilot.matching.scam import assess
from jobpilot.matching.scoring import freshness_points, rank, skill_overlap, stage1, stage2
from jobpilot.services.cost import estimate_cost
from jobpilot.services.matches import MatchView
from jobpilot.services.preferences import FeedbackWeights, SalaryPref
from tests import factories
from tests.factories import NOW, job
from tests.fakes import scores_responder

W = FeedbackWeights()


# ------------------------------------------------------------------ normalise
def test_normalize_helpers() -> None:
    assert (
        html_to_text("&lt;p&gt;Hi&lt;/p&gt;&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;/ul&gt;") == "Hi\nA"
    )
    assert norm_company("Acme Technologies Pvt. Ltd.") == norm_company("ACME")
    assert employment_type("FULLTIME") == "full_time"
    assert employment_type(None, title="SDE Intern") == "internship"
    assert title_level("Sr. Backend Engineer") == "senior"
    assert title_level("Staff Engineer") == "lead"
    assert title_level("Graduate Engineer Trainee") == "junior"
    assert required_years("We need 3+ years of Python and 5 years ago we started") == 3
    assert parse_datetime(1763452800000) == parse_datetime(1763452800)
    assert parse_datetime("garbage") is None


def test_locations() -> None:
    assert find_city("Gurgaon, Haryana") == "Delhi NCR"
    assert find_city("Remote - Bangalore") == "Bengaluru"
    assert detect_work_mode("Bengaluru (Hybrid)") == "hybrid"
    assert remote_allows_country("Worldwide", "India")
    assert remote_allows_country("APAC", "India")
    assert not remote_allows_country("USA only", "India")


# ------------------------------------------------------------------ dedupe
def test_dedupe_keeps_best_link() -> None:
    a = job(
        source="adzuna",
        link_kind="aggregator",
        company="Acme Pvt Ltd",
        title="Backend Engineer Intern (Python)",
        apply_url="https://adzuna/x",
    )
    b = job(company="Acme", title="Backend Engineer Intern - Python")  # ATS, fuzzy same
    c = job(title="Data Analyst Intern", company="Acme")
    out = dedupe([a, b, c])
    assert len(out) == 2
    assert any(j.link_kind == "ats" and "Backend" in j.title for j in out)


# ------------------------------------------------------------------ filters
@pytest.mark.parametrize(
    ("overrides", "prefs_overrides", "reason"),
    [
        ({"posted_at": NOW - timedelta(days=30)}, {}, "stale"),
        ({"employment_type": "contract"}, {}, "job_type"),
        ({"remote_type": "onsite"}, {"work_modes": ["remote"]}, "work_mode"),
        ({"remote_type": "remote", "remote_region": "USA only", "city": None}, {}, "remote_region"),
        ({"city": "Pune", "location": "Pune"}, {}, "location"),
        ({"company": "Byju's"}, {"excluded_companies": ["Byjus"]}, None),
        ({"title": "Sales Intern"}, {"excluded_keywords": ["sales"]}, "excluded_keyword"),
        ({}, {"must_have_keywords": ["Rust"]}, "missing_must_have"),
        ({"title": "Senior Staff Engineer", "employment_type": "full_time"}, {}, "seniority"),
        ({"description": "Requires 6+ years of backend experience. " * 5}, {}, "seniority"),
        (
            {"salary_max": 20000, "currency": "INR", "salary_period": "month"},
            {"min_salary": SalaryPref(amount=600000, currency="INR", period="year", raw="6 LPA")},
            "salary",
        ),
    ],
)
def test_hard_filters(overrides, prefs_overrides, reason) -> None:
    p = factories.prefs(**prefs_overrides)
    assert (
        drop_reason(job(**overrides), factories.profile(), p, W, now=NOW, max_age_days=14) == reason
    )


def test_relocation_and_estimated_salary_rules() -> None:
    p = factories.prefs(willing_to_relocate=True)
    pune = job(city="Pune", location="Pune")
    assert drop_reason(pune, factories.profile(), p, W, now=NOW, max_age_days=14) is None
    strict = FeedbackWeights(location_strict=True)
    assert drop_reason(pune, factories.profile(), p, strict, now=NOW, max_age_days=14) == "location"
    est = job(salary_max=100, currency="INR", salary_period="year", salary_is_estimated=True)
    p2 = factories.prefs(
        min_salary=SalaryPref(amount=600000, currency="INR", period="year", raw="6")
    )
    assert drop_reason(est, factories.profile(), p2, W, now=NOW, max_age_days=14) is None
    report = apply_filters(
        [job(), job(posted_at=NOW - timedelta(days=40))],
        factories.profile(),
        factories.prefs(),
        W,
        now=NOW,
        max_age_days=14,
    )
    assert len(report.kept) == 1 and report.dropped["stale"] == 1


# ------------------------------------------------------------------ scam
def test_scam_rules() -> None:
    assert assess(
        job(description="Pay a registration fee of Rs 2000 to join. " * 8, link_kind="aggregator"),
        candidate_level="fresher",
    ).drop
    assert assess(job(company="Unknown company", link_kind="aggregator"), candidate_level="x").drop
    wa = assess(
        job(description="Contact on WhatsApp only to apply. " * 10, link_kind="aggregator"),
        candidate_level="fresher",
    )
    assert wa.drop
    v = assess(job(description="short", source="greenhouse"), candidate_level="intern")
    assert not v.drop and "very short description" in v.warnings
    rich = assess(
        job(salary_max=9_000_000, currency="INR", salary_period="year"), candidate_level="intern"
    )
    assert "unusually high pay for the level" in rich.warnings


# ------------------------------------------------------------------ scoring
async def test_stage1_prefers_relevant_jobs_and_feedback_moves_it() -> None:
    profile, prefs = factories.profile(), factories.prefs()
    good = job(title="Backend Engineer Intern (Python)")
    bad = job(
        title="Mechanical Design Intern", description="SolidWorks, AutoCAD, CNC machines. " * 10
    )
    ranked = await stage1(profile, prefs, W, [bad, good], HashEmbedder())
    assert ranked[0].job is good and ranked[0].stage1 > ranked[1].stage1
    assert "FastAPI" in ranked[0].matched_skills
    penalised = FeedbackWeights(penalty_terms={"backend": 0.3})
    again = await stage1(profile, prefs, penalised, [good], HashEmbedder())
    assert again[0].stage1 < ranked[0].stage1


def test_skill_overlap_weights_evidence() -> None:
    score, matched, missing = skill_overlap(
        factories.profile(),
        job(title="Backend Engineer", description="FastAPI, Redis and Kubernetes required"),
    )
    assert set(matched) == {"FastAPI", "Redis"} and missing == ["Kubernetes"]
    assert 0 < score < 1


async def test_stage2_clamps_and_computes_total_in_code() -> None:
    profile, prefs = factories.profile(), factories.prefs()
    items = await stage1(profile, prefs, W, [job(), job(title="Backend Developer")], HashEmbedder())
    llm = FakeLLM({"JobScores": scores_responder(score=99)})  # 99 > 40 cap
    n = await stage2(llm, profile, prefs, items, now=NOW)
    assert n == 2
    s = items[0]
    assert s.sub_scores["skills_match"] == 40  # clamped
    assert s.total == 40 + 17 + 13 + 9 + 8 + freshness_points(s.job.posted_at, NOW)
    assert not s.is_basic and s.pitch and s.why_fit and s.missing_skills == ["Kubernetes"]


async def test_stage2_falls_back_to_basic_when_llm_down_or_budget_spent() -> None:
    profile, prefs = factories.profile(), factories.prefs()
    items = await stage1(profile, prefs, W, [job() for _ in range(7)], HashEmbedder())
    assert await stage2(OfflineLLM(), profile, prefs, items, now=NOW) == 0
    assert all(s.is_basic and s.total > 0 for s in items)
    token = set_meter(UsageMeter(max_tokens=1))  # spent budget
    try:
        llm = FakeLLM({"JobScores": scores_responder()})
        fresh = await stage1(profile, prefs, W, [job() for _ in range(7)], HashEmbedder())
        scored = await stage2(llm, profile, prefs, fresh, now=NOW)
    finally:
        reset_meter(token)
    assert scored == 5 and sum(s.is_basic for s in fresh) == 2  # 1st batch ran, then budget hit


async def test_rank_by_score_then_recency() -> None:
    items = await stage1(
        factories.profile(),
        factories.prefs(),
        W,
        [job(posted_at=NOW - timedelta(days=d)) for d in (5, 1)],
        HashEmbedder(),
    )
    for s in items:
        s.total = 70
    assert rank(items)[0].job.posted_at == NOW - timedelta(days=1)


# ------------------------------------------------------------------ links
async def test_link_checker(sessions) -> None:
    async with httpx.AsyncClient() as client:
        checker = LinkChecker(client, sessions)
        with respx.mock() as router:
            router.get("https://x.test/dead").respond(404)
            router.get("https://x.test/walled").respond(403)
            ok = router.get("https://x.test/ok").respond(200)
            jobs = [
                job(apply_url="https://x.test/dead", link_kind="aggregator"),
                job(apply_url="https://x.test/walled", link_kind="aggregator"),
                job(apply_url="https://x.test/ok", link_kind="aggregator"),
                job(apply_url="https://ats.test/1", link_kind="ats"),
            ]  # never checked
            alive, dead = await checker.alive(jobs)
            assert dead == 1 and len(alive) == 3
            await checker.verdict("https://x.test/ok")  # cached
            assert ok.call_count == 1


# ------------------------------------------------------------------ cost + cards
def test_cost_estimates() -> None:
    assert estimate_cost("claude-haiku-5-5", Usage(1_000_000, 100_000)) == pytest.approx(0.15)
    assert estimate_cost("unknown-model", Usage(1_000_000, 0)) == pytest.approx(1.0)
    assert estimate_cost("none", Usage(5, 5)) == 0


def _view(**kw: Any) -> MatchView:
    base: dict[str, Any] = dict(
        match_id=7,
        job_id=1,
        title="Backend Engineer (Python)",
        company="Acme",
        location="Hyderabad",
        remote_type="hybrid",
        employment_type="full_time",
        salary_min=1_200_000,
        salary_max=1_800_000,
        currency="INR",
        salary_period="year",
        salary_is_estimated=False,
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source="greenhouse",
        posted_at=datetime(2026, 11, 19, tzinfo=UTC),
        score=87,
        is_basic=False,
        status=MatchStatus.SENT,
        why_fit=["FastAPI + PostgreSQL match"],
        missing_skills=["Kubernetes", "Kafka"],
        red_flags=[],
        pitch="Lead with automation.",
    )
    base.update(kw)
    return MatchView(**base)


def test_job_card_matches_spec_format() -> None:
    text = job_card(_view(), "Greenhouse", None, now=datetime(2026, 11, 21, tzinfo=UTC))
    assert text.startswith("🟢 87% match · <b>Backend Engineer (Python)</b>")
    assert "🏢 Acme · 📍 Hyderabad (Hybrid) · 💼 Full-time" in text
    assert "💰 ₹12–18 LPA · 🕒 Posted 2 days ago · via Greenhouse" in text
    assert "<b>Gaps:</b> Kubernetes, Kafka" in text and "<b>Pitch:</b>" in text


def test_job_card_escapes_and_respects_length_limit() -> None:
    evil = _view(
        title="<script>alert(1)</script>" * 30,
        company="A&B <Co>",
        why_fit=["x" * 900] * 3,
        pitch="<b>" * 300,
        red_flags=["⚠ very short description"],
        score=62,
        is_basic=True,
    )
    text = job_card(evil, "Adzuna", "https://www.adzuna.in")
    assert "<script>" not in text and "A&amp;B &lt;Co&gt;" in text
    assert utf16_len(text) <= TELEGRAM_MESSAGE_LIMIT
    assert '<a href="https://www.adzuna.in">Adzuna</a>' in text
    assert "⚪ 62% basic match" in text and "Check carefully" in text


def test_badges_salary_and_age() -> None:
    assert job_card(_view(score=70), "x", None).startswith("🟡")
    assert job_card(_view(score=61), "x", None).startswith("🟠")
    assert format_salary(None, None, None, None) == "Not disclosed"
    assert posted_ago(None) == "Posted date unknown"


def test_card_keyboards_fit_callback_limits() -> None:
    kb = card_keyboard(_view())
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert buttons[0].url and buttons[0].text == "🚀 Apply"
    for b in buttons[1:] + [b for row in reason_keyboard(123456789).inline_keyboard for b in row]:
        assert b.callback_data and len(b.callback_data.encode()) <= 64
    assert buttons[1].callback_data is not None
    assert JobCb.unpack(buttons[1].callback_data).a is JAction.SAVE


def test_callback_data_never_needs_reserved_separator() -> None:
    """aiogram reserves ':' in callback data; every value we encode must avoid it."""
    from jobpilot.bot.handlers.jobs import TimeCb

    TimeCb(hhmm="0730").pack()
    with pytest.raises(ValueError, match="Separator"):
        TimeCb(hhmm="07:30").pack()
    for code, _label in REASONS:
        assert ":" not in JobCb(a=JAction.REASON, m=1, r=code).pack().split(":", 3)[3]
