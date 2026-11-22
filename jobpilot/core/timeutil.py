"""Time helpers. Rule: store UTC, compute in the user's zone, display in the user's zone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite drops tzinfo) and convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_local_occurrence(local_time: time, tz_name: str, now: datetime | None = None) -> datetime:
    """Next instant (UTC) when the wall clock in `tz_name` shows `local_time`, strictly after now.

    Handles DST gaps by normalising through UTC, so a non-existent local time moves forward.
    """
    tz = ZoneInfo(tz_name)
    now_utc = ensure_utc(now or utcnow())
    local_now = now_utc.astimezone(tz)
    candidate_day: date = local_now.date()
    for _ in range(3):
        candidate = datetime.combine(candidate_day, local_time, tzinfo=tz)
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > now_utc:
            return candidate_utc
        candidate_day += timedelta(days=1)
    raise RuntimeError("unreachable: no occurrence found in 3 days")  # pragma: no cover


def format_local(value: datetime, tz_name: str, fmt: str = "%a %d %b, %H:%M") -> str:
    return ensure_utc(value).astimezone(ZoneInfo(tz_name)).strftime(fmt)
