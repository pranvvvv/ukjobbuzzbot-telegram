"""aiogram FSM storage backed by SQLAlchemy (ADR-005).

aiogram ships Memory, Redis and Mongo storages only. This one keeps conversation state
in the app's own database, so a laptop restart mid-onboarding resumes exactly where
the user left off (spec Sections 4 and 10).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, KeyBuilder, StorageKey
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import FsmState


class SQLStorage(BaseStorage):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        key_builder: KeyBuilder | None = None,
    ) -> None:
        self._sessions = sessions
        self._keys = key_builder or DefaultKeyBuilder(
            with_bot_id=True, with_destiny=True, with_business_connection_id=True
        )

    # ------------------------------------------------------------- state
    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._upsert(self._keys.build(key), {"state": value})

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._sessions() as session:
            row = await session.get(FsmState, self._keys.build(key))
            return row.state if row else None

    # -------------------------------------------------------------- data
    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = dict(data)
        try:
            json.dumps(payload)
        except TypeError as exc:
            raise TypeError(f"FSM data must be JSON-serialisable: {exc}") from exc
        await self._upsert(self._keys.build(key), {"data": payload})

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._sessions() as session:
            row = await session.get(FsmState, self._keys.build(key))
            return dict(row.data) if row and row.data else {}

    async def close(self) -> None:
        # The engine is owned and disposed by the application runner.
        return None

    # ----------------------------------------------------------- helpers
    async def _upsert(self, key: str, values: dict[str, Any]) -> None:
        now = utcnow()
        async with self._sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            row = {"key": key, "state": None, "data": {}, "updated_at": now, **values}
            stmt = insert(FsmState).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FsmState.key], set_={**values, "updated_at": now}
            )
            await session.execute(stmt)
            # Housekeeping: a finished conversation leaves no row behind. (Checked in
            # Python: JSON equality isn't portable across SQLite and Postgres.)
            stored = await session.get(FsmState, key, populate_existing=True)
            if stored is not None and stored.state is None and not stored.data:
                await session.execute(delete(FsmState).where(FsmState.key == key))
