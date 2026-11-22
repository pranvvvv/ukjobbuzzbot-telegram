from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select

from jobpilot.bot.fsm_storage import SQLStorage
from jobpilot.db.engine import create_engine, create_session_factory
from jobpilot.db.models import FsmState

pytestmark = pytest.mark.integration


class Onboarding(StatesGroup):
    question = State()


KEY = StorageKey(bot_id=123456, chat_id=111, user_id=111)


async def test_state_and_data_survive_a_restart(settings, sessions) -> None:
    storage = SQLStorage(sessions)
    await storage.set_state(KEY, Onboarding.question)
    await storage.set_data(KEY, {"question_index": 3, "answers": {"job_type": ["full_time"]}})

    # "Restart": brand-new engine, session factory and storage over the same DB file.
    engine = create_engine(settings.resolved_database_url)
    restarted = SQLStorage(create_session_factory(engine))
    assert await restarted.get_state(KEY) == "Onboarding:question"
    assert await restarted.get_data(KEY) == {
        "question_index": 3,
        "answers": {"job_type": ["full_time"]},
    }
    await engine.dispose()


async def test_update_data_merges(sessions) -> None:
    storage = SQLStorage(sessions)
    await storage.set_data(KEY, {"a": 1})
    merged = await storage.update_data(KEY, {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert await storage.get_value(KEY, "b") == 2


async def test_keys_are_isolated_per_user(sessions) -> None:
    storage = SQLStorage(sessions)
    other = StorageKey(bot_id=123456, chat_id=222, user_id=222)
    await storage.set_state(KEY, "A:x")
    assert await storage.get_state(other) is None
    assert await storage.get_data(other) == {}


async def test_clearing_removes_the_row(sessions) -> None:
    storage = SQLStorage(sessions)
    await storage.set_state(KEY, Onboarding.question)
    await storage.set_data(KEY, {"x": 1})
    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})
    async with sessions() as s:
        assert (await s.execute(select(func.count()).select_from(FsmState))).scalar() == 0


async def test_non_json_data_rejected(sessions) -> None:
    with pytest.raises(TypeError, match="JSON-serialisable"):
        await SQLStorage(sessions).set_data(KEY, {"when": datetime.now(UTC)})
