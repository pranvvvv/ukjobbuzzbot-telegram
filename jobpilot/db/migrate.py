"""Programmatic Alembic entry points (used by `run`, `migrate`, `doctor` and tests)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from jobpilot.db.engine import create_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade_to_head(url: str) -> None:
    """Blocking: run from a thread when called inside the event loop (Alembic env is async)."""
    command.upgrade(alembic_config(url), "head")


async def upgrade_to_head_async(url: str) -> None:
    await asyncio.to_thread(upgrade_to_head, url)


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


async def current_revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:

            def _rev(sync_conn: Connection) -> str | None:
                return MigrationContext.configure(sync_conn).get_current_revision()

            return await conn.run_sync(_rev)
    finally:
        await engine.dispose()
