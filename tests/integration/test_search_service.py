from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from jobpilot.db.models import Match, Run, RunType
from jobpilot.services.preferences import PreferenceService
from jobpilot.services.search import NotReadyError, SearchInProgressError
from jobpilot.sources.base import QuotaExceededError
from jobpilot.sources.registry import SourceSet
from tests import factories
from tests.fakes import StubSource, queries_responder, scores_responder

pytestmark = pytest.mark.integration


def _recent(**kw):
    return factories.job(posted_at=datetime.now(UTC) - timedelta(days=1), **kw)


async def _setup(container, fake_llm, sources):
    fake_llm._responses.update({"QueryPlan": queries_responder, "JobScores": scores_responder()})
    container.search.d.sources = SourceSet(sources, frozenset())
    await container.users.register_or_get(111, "Pranav")
    pid = await container.profiles.save_draft(111, "resume text", factories.profile())
    await container.profiles.activate(111, pid)
    await PreferenceService(container.sessions).save(111, pid, factories.prefs())


async def test_full_run_scores_stores_and_never_repeats(container, fake_llm) -> None:
    good = [_recent(title=f"Backend Engineer Intern {i}", company=f"Co{i}") for i in range(3)]
    broken = StubSource("adzuna", fail=RuntimeError("boom"))
    skipped = StubSource("jsearch", fail=QuotaExceededError("monthly quota reached"))
    await _setup(container, fake_llm, [StubSource("greenhouse", good), broken, skipped])
    progress: list[str] = []

    async def say(text: str) -> None:
        progress.append(text)

    out = await container.search.run(111, RunType.SEARCH, limit=10, progress=say)
    assert len(out.to_send) == 3 and not out.basic_only
    assert out.errors.keys() == {"adzuna"} and out.skipped.keys() == {"jsearch"}
    assert not out.all_sources_failed and out.used_ai_queries
    assert out.counts["found"] == 3 and out.counts["strong"] == 3
    assert progress and "Searching 3 sources" in progress[0]
    for v in out.to_send:
        await container.matches.mark_sent(v.match_id, 111)
    again = await container.search.run(111, RunType.DIGEST, limit=10)
    assert again.to_send == [] and again.counts["already_seen"] == 3
    async with container.sessions() as s:
        runs = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
        assert runs == 2
        assert (await s.execute(select(func.count()).select_from(Match))).scalar_one() == 3


async def test_all_sources_failing_is_reported(container, fake_llm) -> None:
    await _setup(
        container,
        fake_llm,
        [StubSource("a", fail=RuntimeError("x")), StubSource("b", fail=TimeoutError())],
    )
    out = await container.search.run(111, RunType.DIGEST, limit=5)
    assert out.all_sources_failed and out.to_send == []


async def test_llm_down_gives_basic_matches(container, fake_llm) -> None:
    await _setup(container, fake_llm, [StubSource("greenhouse", [_recent()])])
    fake_llm._responses.pop("JobScores")  # scoring fails → basic
    container.search.d.threshold = 10
    out = await container.search.run(111, RunType.SEARCH, limit=5)
    assert out.basic_only and out.to_send and out.to_send[0].is_basic


async def test_not_ready_and_lock(container, fake_llm) -> None:
    with pytest.raises(NotReadyError):
        await container.search.run(999, RunType.SEARCH, limit=5)
    await _setup(container, fake_llm, [StubSource("greenhouse", [_recent()])])
    lock = container.search._locks.setdefault(111, __import__("asyncio").Lock())
    async with lock:
        assert container.search.is_running(111)
        with pytest.raises(SearchInProgressError):
            await container.search.run(111, RunType.SEARCH, limit=5)
