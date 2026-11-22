"""Remaining behaviour: query fallback, embedder fallback, budget guard, corrections, commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from jobpilot.bot.handlers.jobs import PageCb, TimeCb
from jobpilot.bot.handlers.onboarding import PAction, ProfileCb
from jobpilot.bot.questions_ui import QAction, QCb
from jobpilot.container import Container
from jobpilot.db.models import Job, Match, MatchStatus, Run, RunType, User
from jobpilot.llm.embeddings import FastEmbedder
from jobpilot.llm.fake import FakeLLM, OfflineLLM
from jobpilot.resume.schema import ProfileExtraction
from jobpilot.services.cost import CostGuard
from jobpilot.services.preferences import PreferenceService
from jobpilot.services.profile import ProfileService
from jobpilot.services.queries import build_queries, fallback_queries
from jobpilot.services.users import UserService
from tests import factories
from tests.conftest import OWNER_ID
from tests.fakes import callback_update, message_update, queries_responder

pytestmark = pytest.mark.integration


def test_fallback_queries_cover_roles_cities_and_remote() -> None:
    qs = fallback_queries(factories.profile(), factories.prefs(), limit=8)
    keywords = [q.keywords for q in qs]
    assert "Backend Developer intern" in keywords  # internship wanted
    assert any(q.remote for q in qs) and any(q.location == "Hyderabad" for q in qs)
    assert len({(q.keywords, q.location, q.remote) for q in qs}) == len(qs)
    remote_only = fallback_queries(
        factories.profile(), factories.prefs(work_modes=["remote"]), limit=5
    )
    assert all(q.location is None for q in remote_only)


async def test_build_queries_ai_and_fallback() -> None:
    qs, used_ai = await build_queries(
        FakeLLM({"QueryPlan": queries_responder}), factories.profile(), factories.prefs(), limit=6
    )
    assert used_ai and qs[0].keywords == "backend developer python"
    qs, used_ai = await build_queries(OfflineLLM(), factories.profile(), factories.prefs(), limit=6)
    assert not used_ai and qs


async def test_embedder_falls_back_when_model_cannot_load(tmp_path: Path) -> None:
    emb = FastEmbedder("no-such/model-name", tmp_path)
    vecs = await emb.embed(["python developer", "data analyst"])
    assert emb.name == "hash-embedder" and len(vecs) == 2 and len(vecs[0]) == emb.dimensions


async def test_daily_budget_shrinks_the_run_cap(sessions) -> None:
    guard = CostGuard(sessions, "claude-haiku-5-5", per_run_tokens=150_000, daily_budget_usd=0.01)
    # blended Haiku price: 0.8×$0.10 + 0.2×$0.50 = $0.18/M tokens → $0.01 buys 55,555
    assert (await guard.meter_for_run()).max_tokens == 55_555
    async with sessions.begin() as s:
        s.add(Run(type=RunType.SEARCH, started_at=datetime.now(UTC), est_cost_usd=0.02))
    assert await guard.spent_today() == pytest.approx(0.02)
    assert (await guard.meter_for_run()).max_tokens == 0  # budget spent → basic scoring


async def test_ai_correction_path(sessions, settings) -> None:
    corrected = factories.profile(location_current="Pune").model_dump(
        include=set(ProfileExtraction.model_fields)
    )
    svc = ProfileService(sessions, FakeLLM({"ProfileExtraction": [corrected]}))
    await UserService(sessions, settings).register_or_get(111, "P")
    resume = "Python FastAPI PostgreSQL Docker React Django Redis LangChain RAG Git JavaScript SQL"
    pid = await svc.save_draft(111, resume, factories.profile())
    fixed = await svc.correct(pid, "I moved to Pune")
    assert fixed.location_current == "Pune"
    assert "FastAPI" in fixed.skills.all()  # evidence still checked against the resume


async def _ready(container: Container) -> int:
    await container.users.register_or_get(OWNER_ID, "Pranav")
    pid = await container.profiles.save_draft(OWNER_ID, "resume", factories.profile())
    await container.profiles.activate(OWNER_ID, pid)
    await PreferenceService(container.sessions).save(OWNER_ID, pid, factories.prefs())
    return pid


async def test_commands_pause_resume_settime_stats_pages(
    dp, bot, telegram, container, sessions
) -> None:
    await _ready(container)
    for text in ("/resume", "/pause", "/settime 7:45am", "/settime nonsense", "/settime"):
        await dp.feed_update(bot, message_update(OWNER_ID, text))
    texts = telegram.sent_texts()
    assert "Daily digest on" in texts[0] and "paused" in texts[1]
    assert "07:45" in texts[2] and "like /settime 08:30" in texts[3]
    await dp.feed_update(bot, callback_update(OWNER_ID, TimeCb(hhmm="2100").pack()))
    assert "21:00" in telegram.of_type(EditMessageText)[-1].text
    async with sessions() as s:
        assert (await s.get(User, OWNER_ID)).digest_time.strftime("%H:%M") == "21:00"
    # history pagination
    async with sessions.begin() as s:
        for i in range(12):
            job_row = Job(
                fingerprint=f"fp{i}",
                source="greenhouse",
                source_job_id=str(i),
                title=f"Role {i}",
                company="Co",
                apply_url=f"https://x.test/{i}",
                description="",
                raw_json={},
            )
            s.add(job_row)
            await s.flush()
            s.add(
                Match(
                    user_id=OWNER_ID,
                    job_id=job_row.id,
                    profile_version=1,
                    score=70,
                    status=MatchStatus.SENT,
                    sent_at=datetime.now(UTC) - timedelta(minutes=i),
                )
            )
    await dp.feed_update(bot, message_update(OWNER_ID, "/history"))
    page1 = telegram.of_type(SendMessage)[-1]
    assert "(1–10 of 12)" in page1.text and page1.reply_markup is not None
    await dp.feed_update(bot, callback_update(OWNER_ID, PageCb(kind="history", offset=10).pack()))
    assert "(11–12 of 12)" in telegram.of_type(EditMessageText)[-1].text
    await dp.feed_update(bot, message_update(OWNER_ID, "/saved"))
    assert "Nothing here yet" in telegram.sent_texts()[-1]
    await dp.feed_update(bot, message_update(OWNER_ID, "/stats"))
    assert "greenhouse" in telegram.sent_texts()[-1]


async def test_preferences_edit_menu_and_back(dp, bot, telegram, container) -> None:
    await _ready(container)
    await dp.feed_update(bot, message_update(OWNER_ID, "/preferences"))
    assert "Your preferences" in telegram.of_type(SendMessage)[-1].text
    await dp.feed_update(bot, callback_update(OWNER_ID, QCb(a=QAction.EDIT, v=-1).pack()))
    assert "What would you like to change" in telegram.of_type(EditMessageText)[-1].text
    await dp.feed_update(
        bot, callback_update(OWNER_ID, QCb(a=QAction.EDIT, v=1).pack())
    )  # job types
    assert "What type of job" in telegram.of_type(EditMessageText)[-1].text
    await dp.feed_update(bot, callback_update(OWNER_ID, QCb(a=QAction.BACK).pack()))
    assert "Which roles" in telegram.of_type(EditMessageText)[-1].text
    await dp.feed_update(bot, message_update(OWNER_ID, "free text where buttons are expected"))
    assert "tap one of the buttons" in telegram.sent_texts()[-1]
    await dp.feed_update(bot, callback_update(OWNER_ID, QCb(a=QAction.DONE).pack()))
    assert (
        "Your preferences" in telegram.of_type(EditMessageText)[-1].text
    )  # editing → back to plan
    await dp.feed_update(bot, callback_update(OWNER_ID, QCb(a=QAction.PLAN).pack()))
    assert telegram.of_type(AnswerCallbackQuery)


async def test_fix_profile_flow_without_ai(dp, bot, telegram, container) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    pid = await container.profiles.save_draft(OWNER_ID, "resume", factories.profile())
    state = dp.fsm.get_context(bot, OWNER_ID, OWNER_ID)
    await state.set_state("Flow:confirming_profile")
    await state.update_data(draft_profile_id=pid)
    await dp.feed_update(bot, callback_update(OWNER_ID, ProfileCb(a=PAction.FIX).pack()))
    assert "Tell me what to change" in telegram.sent_texts()[-1]
    await dp.feed_update(bot, message_update(OWNER_ID, "I'm in Pune, add Kubernetes"))
    card = telegram.sent_texts()[-1]
    assert "Pune" in card and "Does this look right" in card
