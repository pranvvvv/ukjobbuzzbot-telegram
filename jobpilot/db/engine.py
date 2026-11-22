"""Async engine + session factory with SQLite tuned for a long-running local service."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",  # readers don't block the writer
    "PRAGMA synchronous=NORMAL",  # safe with WAL, much faster than FULL
    "PRAGMA foreign_keys=ON",  # SQLite ignores FKs unless asked
    "PRAGMA busy_timeout=5000",  # wait instead of failing on brief lock contention
)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in SQLITE_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
