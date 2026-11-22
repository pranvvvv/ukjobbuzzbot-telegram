from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from jobpilot.db.engine import create_engine
from jobpilot.db.migrate import (
    alembic_config,
    current_revision,
    head_revision,
    upgrade_to_head_async,
)
from jobpilot.db.models import Base

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "users",
    "profiles",
    "preferences",
    "jobs",
    "matches",
    "feedback",
    "runs",
    "api_usage",
    "fsm_state",
    "http_cache",
    "link_cache",
}


async def test_upgrade_creates_every_table_and_matches_models(settings) -> None:
    settings.ensure_dirs()
    url = settings.resolved_database_url
    await upgrade_to_head_async(url)
    assert await current_revision(url) == head_revision(url)

    engine = create_engine(url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    await engine.dispose()
    assert tables >= EXPECTED_TABLES
    assert diff == [], f"models and migrations drifted: {diff}"


async def test_sqlite_pragmas_applied(sessions) -> None:
    async with sessions() as s:
        assert (await s.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


async def test_downgrade_to_base_and_back(settings) -> None:
    settings.ensure_dirs()
    url = settings.resolved_database_url
    await upgrade_to_head_async(url)
    await asyncio.to_thread(command.downgrade, alembic_config(url), "base")
    assert await current_revision(url) is None
    await upgrade_to_head_async(url)
    assert await current_revision(url) == head_revision(url)
