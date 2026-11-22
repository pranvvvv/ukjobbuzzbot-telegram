"""The whole Telegram journey through the real dispatcher (fake Telegram, fake AI)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import Document
from sqlalchemy import select

from jobpilot.bot.app import build_dispatcher
from jobpilot.bot.delivery import JAction, JobCb
from jobpilot.bot.handlers.onboarding import PAction, ProfileCb
from jobpilot.bot.questions_ui import QAction, QCb
from jobpilot.bot.states import Flow
from jobpilot.db.models import Match, MatchStatus, User
from jobpilot.sources.registry import SourceSet
from tests import factories
from tests.conftest import OWNER_ID
from tests.fakes import (
    StubSource,
    callback_update,
    extraction_responder,
    message_update,
    queries_responder,
    resume_check_responder,
    scores_responder,
)

pytestmark = pytest.mark.integration
RESUME = (Path(__file__).parents[1] / "fixtures" / "resumes" / "fresher_backend.txt").read_text()


@pytest.fixture(autouse=True)
def _ai(fake_llm, container) -> None:
    fake_llm._responses.update(
        {
            "ResumeCheck": resume_check_responder,
            "ProfileExtraction": extraction_responder,
            "QueryPlan": queries_responder,
            "JobScores": scores_responder(),
        }
    )
    jobs = [
        factories.job(
            title=f"Backend Engineer Intern {i}",
            company=f"Co{i}",
            posted_at=datetime.now(UTC) - timedelta(days=1),
        )
        for i in range(3)
    ]
    container.search.d.sources = SourceSet(
        [StubSource("greenhouse", jobs, label="Greenhouse")], frozenset()
    )


async def _settle(telegram, marker: str = "") -> None:
    """Wait (up to 5s) for the paste debounce to fire and the resume to be processed."""
    for _ in range(250):
        await asyncio.sleep(0.02)
        edits = telegram.of_type(EditMessageText)
        if edits and (not marker or marker in edits[-1].text) and "Reading" not in edits[-1].text:
            return


@pytest.fixture(autouse=True)
def _realistic_debounce(dp) -> None:
    dp["paste_debounce"] = 0.3  # longer than handling one update, like the real 3s window


def _last_markup_data(telegram) -> list[str]:
    for m in reversed(telegram.requests):
        markup = getattr(m, "reply_markup", None)
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.callback_data or b.url for row in markup.inline_keyboard for b in row]
    return []


async def _cb(dp, bot, data) -> None:
    await dp.feed_update(bot, callback_update(OWNER_ID, data))


async def test_resume_to_cards_journey_with_restart(dp, bot, telegram, container, sessions) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    # resume pasted in two parts → merged after the debounce
    half = len(RESUME) // 2
    await dp.feed_update(bot, message_update(OWNER_ID, RESUME[:half]))
    await dp.feed_update(bot, message_update(OWNER_ID, RESUME[half:]))
    await _settle(telegram, "understood")
    card = telegram.of_type(EditMessageText)[-1].text
    assert "Here's what I understood" in card and "Aarav Mehta" in card and "Hyderabad" in card
    await _cb(dp, bot, ProfileCb(a=PAction.CONFIRM).pack())
    q1 = telegram.of_type(SendMessage)[-1].text
    assert "Question 1 of" in q1 and "Which roles" in q1
    await _cb(dp, bot, QCb(a=QAction.DONE).pack())  # roles pre-ticked
    await _cb(dp, bot, QCb(a=QAction.DONE).pack())  # job types

    # --- restart: brand-new dispatcher over the same database
    dp2 = build_dispatcher(container)
    dp2["paste_debounce"] = 0.01
    state = await dp2.fsm.storage.get_state(
        __import__("aiogram").fsm.storage.base.StorageKey(
            bot_id=bot.id, chat_id=OWNER_ID, user_id=OWNER_ID
        )
    )
    assert state == Flow.answering.state
    await _cb(dp2, bot, QCb(a=QAction.TOGGLE, v=0).pack())  # untick Remote
    await _cb(dp2, bot, QCb(a=QAction.DONE).pack())  # work modes → hybrid, onsite
    await dp2.feed_update(bot, message_update(OWNER_ID, "Hyderabad, Bengaluru"))  # locations
    await _cb(dp2, bot, QCb(a=QAction.PICK, v=1).pack())  # relocate: no
    for _ in range(4):  # salary, joining, exclude, keywords → skip
        await _cb(dp2, bot, QCb(a=QAction.SKIP).pack())
    await _cb(dp2, bot, QCb(a=QAction.DONE).pack())  # company size: any
    await _cb(dp2, bot, QCb(a=QAction.PICK, v=1).pack())  # 10 per digest
    await _cb(dp2, bot, QCb(a=QAction.CUSTOM).pack())  # digest time: other…
    await dp2.feed_update(bot, message_update(OWNER_ID, "7:30pm"))
    plan = telegram.of_type(SendMessage)[-1].text
    assert "Your search plan" in plan and "Hybrid, On-site" in plan
    assert "Hyderabad, Bengaluru" in plan and "19:30" in plan and "Greenhouse" in plan

    await _cb(dp2, bot, QCb(a=QAction.START).pack())
    texts = telegram.sent_texts()
    cards = [t for t in texts if "% match" in t]
    assert len(cards) == 3 and "via Greenhouse" in cards[0]
    assert any("Search summary" in t for t in texts)
    async with sessions() as s:
        user = await s.get(User, OWNER_ID)
        matches = (await s.execute(select(Match))).scalars().all()
    assert user.digest_enabled and user.digest_time.strftime("%H:%M") == "19:30"
    assert all(m.status is MatchStatus.SENT for m in matches)

    # --- feedback buttons
    mid = matches[0].id
    await _cb(dp2, bot, JobCb(a=JAction.SAVE, m=mid).pack())
    assert "favour roles like" in telegram.of_type(AnswerCallbackQuery)[-1].text
    await _cb(dp2, bot, JobCb(a=JAction.DISMISS, m=matches[1].id).pack())
    await _cb(dp2, bot, JobCb(a=JAction.REASON, m=matches[1].id, r="company").pack())
    assert "Exclude <b>Co" in telegram.sent_texts()[-1]
    await _cb(dp2, bot, JobCb(a=JAction.EXCLUDE_YES, m=matches[1].id).pack())
    prefs = await container.prefs.load(OWNER_ID, (await container.profiles.active(OWNER_ID))[0])
    dismissed = next(m for m in matches if m.id == matches[1].id)
    assert len(prefs[0].excluded_companies) == 1
    assert prefs[0].excluded_companies[0].startswith("Co")
    assert dismissed.job_id
    await dp2.feed_update(bot, message_update(OWNER_ID, "/saved"))
    assert "Backend Engineer Intern" in telegram.sent_texts()[-1]
    await dp2.feed_update(bot, message_update(OWNER_ID, "/stats"))
    assert "JobPilot stats" in telegram.sent_texts()[-1]
    assert telegram.of_type(EditMessageReplyMarkup)


async def test_pdf_upload_and_non_resume(dp, bot, telegram) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    telegram.files["f1"] = (
        Path(__file__).parents[1] / "fixtures" / "resumes" / "fresher_backend.pdf"
    ).read_bytes()
    doc = Document(
        file_id="f1",
        file_unique_id="u1",
        file_name="cv.pdf",
        mime_type="application/pdf",
        file_size=len(telegram.files["f1"]),
    )
    await dp.feed_update(bot, message_update(OWNER_ID, None, document=doc))
    assert "Aarav Mehta" in telegram.of_type(EditMessageText)[-1].text
    await dp.feed_update(bot, message_update(OWNER_ID, "/new"))
    await _cb(dp, bot, ProfileCb(a=PAction.NEW).pack())
    recipe = (
        Path(__file__).parents[1] / "fixtures" / "resumes" / "not_a_resume.txt"
    ).read_text() * 2
    await dp.feed_update(bot, message_update(OWNER_ID, recipe))
    await _settle(telegram, "look like a resume")
    assert "doesn't look like a resume" in telegram.of_type(EditMessageText)[-1].text


async def test_scanned_pdf_and_wrong_type(dp, bot, telegram) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    telegram.files["s"] = (
        Path(__file__).parents[1] / "fixtures" / "resumes" / "scanned.pdf"
    ).read_bytes()
    scan = Document(file_id="s", file_unique_id="u", file_name="scan.pdf", file_size=10)
    img = Document(file_id="i", file_unique_id="v", file_name="me.jpg", file_size=10)
    await dp.feed_update(bot, message_update(OWNER_ID, None, document=scan))
    await dp.feed_update(bot, message_update(OWNER_ID, None, document=img))
    scanned_reply, type_reply = telegram.sent_texts()[-2:]
    assert "looks scanned" in scanned_reply and "PDF, DOCX or TXT" in type_reply


async def test_deleteme_wipes_everything(dp, bot, telegram, sessions) -> None:
    await dp.feed_update(bot, message_update(OWNER_ID, "/start"))
    await dp.feed_update(bot, message_update(OWNER_ID, "/deleteme"))
    await _cb(dp, bot, ProfileCb(a=PAction.DELETE).pack())
    assert "All your data has been deleted" in telegram.of_type(EditMessageText)[-1].text
    async with sessions() as s:
        assert await s.get(User, OWNER_ID) is None
