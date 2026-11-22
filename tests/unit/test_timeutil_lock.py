from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from jobpilot.core.errors import AlreadyRunningError
from jobpilot.core.lock import InstanceLock, lock_is_free
from jobpilot.core.timeutil import ensure_utc, format_local, next_local_occurrence


def test_next_occurrence_later_today_in_ist() -> None:
    now = datetime(2026, 11, 21, 2, 0, tzinfo=UTC)  # 07:30 IST
    nxt = next_local_occurrence(time(9, 0), "Asia/Kolkata", now)
    assert nxt == datetime(2026, 11, 21, 3, 30, tzinfo=UTC)  # 09:00 IST


def test_next_occurrence_rolls_to_tomorrow() -> None:
    now = datetime(2026, 11, 21, 16, 44, tzinfo=UTC)  # 22:14 IST
    nxt = next_local_occurrence(time(9, 0), "Asia/Kolkata", now)
    assert nxt == datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


def test_exactly_now_is_not_next() -> None:
    now = datetime(2026, 11, 21, 3, 30, tzinfo=UTC)
    assert next_local_occurrence(time(9, 0), "Asia/Kolkata", now).day == 22


def test_dst_gap_moves_forward() -> None:
    # 02:30 doesn't exist in New York on 8 Mar 2026; result must still be after now.
    now = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    nxt = next_local_occurrence(time(2, 30), "America/New_York", now)
    assert nxt > now


def test_ensure_utc_and_format() -> None:
    naive = datetime(2026, 1, 1, 12, 0)
    assert ensure_utc(naive).tzinfo is UTC
    assert format_local(datetime(2026, 11, 22, 3, 30, tzinfo=UTC), "Asia/Kolkata") == (
        "Sun 22 Nov, 09:00"
    )


def test_instance_lock_blocks_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "jp.lock"
    with InstanceLock(path):
        assert not lock_is_free(path)
        with pytest.raises(AlreadyRunningError, match="already running"):
            InstanceLock(path).acquire()
    assert lock_is_free(path)
