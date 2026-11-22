from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
import time_machine

from jobpilot.db.models import User
from jobpilot.scheduler.jobs import DigestScheduler
from jobpilot.services.preferences import PreferenceService
from jobpilot.sources.registry import SourceSet
from tests import factories
from tests.fakes import StubSource, queries_responder, scores_responder

pytestmark = pytest.mark.integration


async def _ready_user(container, fake_llm, sources, *, next_at):
    fake_llm._responses.update({"QueryPlan": queries_responder, "JobScores": scores_responder()})
    container.search.d.sources = SourceSet(sources, frozenset())
    await container.users.register_or_get(111, "Pranav")
    pid = await container.profiles.save_draft(111, "resume", factories.profile())
    await container.profiles.activate(111, pid)
    await PreferenceService(container.sessions).save(111, pid, factories.prefs())
    await container.users.apply_digest_settings(111, digest_time=time(9, 0), enable=True)
    async with container.sessions.begin() as s:
        (await s.get(User, 111)).next_digest_at = next_at


async def test_laptop_off_three_days_sends_one_digest(container, fake_llm, bot, telegram) -> None:
    now = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)  # 11:30 IST
    jobs = [factories.job(title="Backend Engineer Intern", posted_at=now - timedelta(days=1))]
    await _ready_user(
        container, fake_llm, [StubSource("greenhouse", jobs)], next_at=now - timedelta(days=3)
    )  # missed 3 digests
    sched = DigestScheduler(bot, container.users, container.delivery)
    with time_machine.travel(now, tick=False):
        assert await sched.tick() == 1
        assert await sched.tick() == 0  # coalesced: no backlog of 3
    texts = telegram.sent_texts()
    assert any("Your daily JobPilot digest" in t for t in texts)
    async with container.sessions() as s:
        user = await s.get(User, 111)
    assert user.next_digest_at == datetime(2026, 11, 22, 3, 30, tzinfo=UTC)  # tomorrow 09:00 IST
    assert user.last_digest_at == now


async def test_no_matches_message_and_sources_down_retry(
    container, fake_llm, bot, telegram
) -> None:
    now = datetime(2026, 11, 21, 4, 0, tzinfo=UTC)
    await _ready_user(container, fake_llm, [StubSource("greenhouse", [])], next_at=now)
    sched = DigestScheduler(bot, container.users, container.delivery)
    with time_machine.travel(now, tick=False):
        await sched.tick()
    assert "No strong new matches today" in telegram.sent_texts()[-1]

    container.search.d.sources = SourceSet(
        [StubSource("x", fail=RuntimeError("down"))], frozenset()
    )
    async with container.sessions.begin() as s:
        (await s.get(User, 111)).next_digest_at = now
    with time_machine.travel(now, tick=False):
        await sched.tick()
    assert "retry your digest in 30 minutes" in telegram.sent_texts()[-1]
    async with container.sessions() as s:
        assert (await s.get(User, 111)).next_digest_at == now + timedelta(minutes=30)


async def test_blocked_user_digest_disabled(container, fake_llm, bot, telegram) -> None:
    from aiogram.methods import SendMessage

    from tests.fakes import error

    now = datetime(2026, 11, 21, 4, 0, tzinfo=UTC)
    await _ready_user(container, fake_llm, [StubSource("greenhouse", [])], next_at=now)
    telegram.handlers[SendMessage] = lambda m: error(403, "Forbidden: bot was blocked by the user")
    with time_machine.travel(now, tick=False):
        await DigestScheduler(bot, container.users, container.delivery).tick()
    async with container.sessions() as s:
        user = await s.get(User, 111)
    assert user.is_blocked and not user.digest_enabled
