"""Shared HTTP for sources: quota manager, response cache, per-source rate limits, retries.

- Quotas: calls per source per UTC day in `api_usage`; month = sum of the month's days.
  A source is skipped (not failed) when it would cross its limit (spec 7).
- Cache: raw responses in `http_cache` for ~6h, so dev runs and repeated digests don't
  burn free-tier quota. Cache keys are hashes; secrets in params never hit the DB.
- Every call: timeout, retries with backoff on 429/5xx/network errors, honouring
  Retry-After, and a logged failure path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.logging import get_logger
from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import ApiUsage, HttpCache
from jobpilot.sources.base import QuotaExceededError, SourceError

log = get_logger(__name__)

USER_AGENT = "JobPilot/0.1 (personal job-search assistant; +https://github.com/)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class SourceLimits:
    per_minute: float = 30.0
    per_day: int | None = None
    per_month: int | None = None
    concurrency: int = 2


class SourceHttp:
    def __init__(
        self,
        client: httpx.AsyncClient,
        sessions: async_sessionmaker[AsyncSession],
        *,
        cache_ttl: timedelta = timedelta(hours=6),
        max_attempts: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._cache_ttl = cache_ttl
        self._max_attempts = max_attempts
        self._backoff = backoff_base
        self._limits: dict[str, SourceLimits] = {}
        self._limiters: dict[str, AsyncLimiter] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.calls: dict[str, int] = {}

    def register(self, source: str, limits: SourceLimits) -> None:
        self._limits[source] = limits
        self._limiters[source] = AsyncLimiter(max(limits.per_minute, 0.1), 60)
        self._sems[source] = asyncio.Semaphore(limits.concurrency)

    def registered(self) -> dict[str, SourceLimits]:
        return dict(self._limits)

    # ------------------------------------------------------------ quota
    async def usage(self, source: str, today: date | None = None) -> tuple[int, int]:
        """(calls today, calls this month) for a source."""
        today = today or utcnow().date()
        month_start = today.replace(day=1)
        async with self._sessions() as session:
            day = await session.get(ApiUsage, (source, today))
            month = (
                await session.execute(
                    select(func.coalesce(func.sum(ApiUsage.calls), 0)).where(
                        ApiUsage.source == source, ApiUsage.day >= month_start
                    )
                )
            ).scalar_one()
        return (day.calls if day else 0), int(month)

    async def check_quota(self, source: str) -> None:
        limits = self._limits.get(source, SourceLimits())
        today_calls, month_calls = await self.usage(source)
        if limits.per_day is not None and today_calls >= limits.per_day:
            raise QuotaExceededError(
                f"{source}: daily quota reached ({today_calls}/{limits.per_day})"
            )
        if limits.per_month is not None and month_calls >= limits.per_month:
            raise QuotaExceededError(
                f"{source}: monthly quota reached ({month_calls}/{limits.per_month})"
            )

    async def _record_call(self, source: str) -> None:
        self.calls[source] = self.calls.get(source, 0) + 1
        today = utcnow().date()
        async with self._sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = insert(ApiUsage).values(source=source, day=today, calls=1)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ApiUsage.source, ApiUsage.day],
                set_={"calls": ApiUsage.calls + 1},
            )
            await session.execute(stmt)

    # ------------------------------------------------------------ cache
    @staticmethod
    def cache_key(url: str, params: dict[str, Any] | None, body: Any = None) -> str:
        blob = json.dumps([url, sorted((params or {}).items()), body], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    async def _cache_get(self, key: str) -> Any | None:
        async with self._sessions() as session:
            row = await session.get(HttpCache, key)
            if row is None or row.expires_at <= utcnow():
                return None
            return json.loads(row.body)

    async def _cache_put(
        self, key: str, source: str, status: int, payload: Any, ttl: timedelta
    ) -> None:
        now = utcnow()
        async with self._sessions.begin() as session:
            row = await session.get(HttpCache, key)
            body = json.dumps(payload, ensure_ascii=False)
            if row is None:
                session.add(
                    HttpCache(
                        key=key,
                        source=source,
                        status_code=status,
                        body=body,
                        fetched_at=now,
                        expires_at=now + ttl,
                    )
                )
            else:
                row.body, row.status_code, row.fetched_at = body, status, now
                row.expires_at = now + ttl

    async def purge_expired(self) -> int:
        from sqlalchemy import delete  # noqa: PLC0415

        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(HttpCache).where(HttpCache.expires_at <= utcnow())
            )
            return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------ request
    async def request_json(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        cache: bool = True,
        cache_ttl: timedelta | None = None,
        quota: bool = True,
    ) -> Any:
        key = self.cache_key(url, params, json_body)
        if cache and (hit := await self._cache_get(key)) is not None:
            log.debug("source.cache_hit", source=source)
            return hit
        if quota:
            await self.check_quota(source)
        limiter = self._limiters.get(source) or AsyncLimiter(30, 60)
        sem = self._sems.get(source) or asyncio.Semaphore(2)
        all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        last: str = ""
        for attempt in range(1, self._max_attempts + 1):
            async with sem, limiter:
                await self._record_call(source)
                try:
                    resp = await self._client.request(
                        method, url, params=params, headers=all_headers, json=json_body
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = f"{type(exc).__name__}"
                    resp = None
            if resp is not None:
                if resp.status_code < 400:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise SourceError(f"{source}: response was not JSON") from exc
                    if cache:
                        await self._cache_put(
                            key, source, resp.status_code, payload, cache_ttl or self._cache_ttl
                        )
                    return payload
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUSES:
                    raise SourceError(f"{source}: {last}")
            if attempt < self._max_attempts:
                delay = self._backoff * 2 ** (attempt - 1)
                if resp is not None and (ra := resp.headers.get("retry-after", "")).isdigit():
                    delay = min(float(ra), 30.0)
                log.warning("source.retrying", source=source, attempt=attempt, error=last)
                await asyncio.sleep(delay)
        raise SourceError(f"{source}: failed after {self._max_attempts} attempts ({last})")

    async def get_json(self, source: str, url: str, **kw: Any) -> Any:
        return await self.request_json(source, "GET", url, **kw)
