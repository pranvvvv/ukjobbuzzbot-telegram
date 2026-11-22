"""Apply-link health (spec 8.7): drop dead links, never over-check.

- ATS links are skipped: a job present in the live board feed is live.
- Aggregator/company links get one streamed GET (many sites reject HEAD), following
  redirects, short timeout. Only 404/410 mean dead; 403/429/timeouts are "unverified"
  and kept (bot walls shouldn't hide real jobs). Results are cached for 24h.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.logging import get_logger
from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import LinkCheck
from jobpilot.matching.normalize import JobPosting
from jobpilot.sources.http import USER_AGENT

log = get_logger(__name__)
DEAD = {404, 410}
CACHE_FOR = timedelta(hours=24)


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


class LinkChecker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        sessions: async_sessionmaker[AsyncSession],
        *,
        concurrency: int = 5,
        timeout: float = 8.0,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout

    async def verdict(self, url: str) -> str:
        key = _hash(url)
        async with self._sessions() as session:
            row = await session.get(LinkCheck, key)
            if row and utcnow() - row.checked_at < CACHE_FOR:
                return row.verdict
        status: int | None = None
        async with self._sem:
            try:
                async with self._client.stream(
                    "GET",
                    url,
                    follow_redirects=True,
                    timeout=self._timeout,
                    headers={"User-Agent": USER_AGENT},
                ) as resp:
                    status = resp.status_code
            except httpx.HTTPError as exc:
                log.debug("link.unverified", error=type(exc).__name__)
        verdict = "dead" if status in DEAD else ("ok" if status and status < 400 else "unverified")
        async with self._sessions.begin() as session:
            row = await session.get(LinkCheck, key)
            if row is None:
                session.add(
                    LinkCheck(
                        url_hash=key,
                        url=url[:2000],
                        status_code=status,
                        verdict=verdict,
                        checked_at=utcnow(),
                    )
                )
            else:
                row.status_code, row.verdict, row.checked_at = status, verdict, utcnow()
        return verdict

    async def alive(self, jobs: list[JobPosting]) -> tuple[list[JobPosting], int]:
        """(jobs whose links aren't dead, number dropped)."""
        to_check = [j for j in jobs if j.link_kind != "ats"]
        verdicts = await asyncio.gather(*(self.verdict(j.apply_url) for j in to_check))
        dead = {id(j) for j, v in zip(to_check, verdicts, strict=True) if v == "dead"}
        return [j for j in jobs if id(j) not in dead], len(dead)
