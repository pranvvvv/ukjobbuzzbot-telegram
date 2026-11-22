from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobpilot.core.errors import ConfigError
from jobpilot.core.lock import InstanceLock
from jobpilot.db.migrate import upgrade_to_head_async
from jobpilot.doctor import Status, exit_code, run_checks
from tests.conftest import TEST_TOKEN

TG = f"https://api.telegram.org/bot{TEST_TOKEN}"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


def _by_name(results):
    return {r.name: r for r in results}


def _mock_telegram(router: respx.Router, me_status: int = 200, webhook: str = "") -> None:
    router.get(f"{TG}/getMe").respond(
        me_status, json={"ok": True, "result": {"id": 123456, "username": "jobpilot_bot"}}
    )
    router.get(f"{TG}/getWebhookInfo").respond(200, json={"ok": True, "result": {"url": webhook}})


@pytest.fixture
def gemini_settings(make_settings):
    return lambda **kw: make_settings(llm_provider="gemini", llm_api_key="AIza-test", **kw)


async def test_healthy_setup_has_no_failures(gemini_settings, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    with respx.mock(assert_all_called=True) as router:
        _mock_telegram(router)
        gemini = router.get(GEMINI).respond(200, json={"models": []})
        results = await run_checks(cwd=tmp_path, settings_loader=gemini_settings)
    checks = _by_name(results)
    assert exit_code(results) == 0
    assert checks["Telegram token"].detail == "@jobpilot_bot (id 123456)"
    assert checks["LLM key (gemini)"].status is Status.OK
    assert gemini.calls.last.request.headers["x-goog-api-key"] == "AIza-test"
    assert checks["Adzuna keys"].status is Status.LATER
    assert checks["Database"].status is Status.OK
    assert all(r.status in {Status.OK, Status.LATER} for r in results)


async def test_rejected_token_and_key_fail(gemini_settings, tmp_path: Path) -> None:
    with respx.mock() as router:
        router.get(f"{TG}/getMe").respond(401, json={"ok": False})
        router.get(GEMINI).respond(403)
        results = await run_checks(cwd=tmp_path, settings_loader=gemini_settings)
    checks = _by_name(results)
    assert checks["Telegram token"].status is Status.FAIL
    assert checks["LLM key (gemini)"].status is Status.FAIL
    assert checks[".env file"].status is Status.WARN
    assert exit_code(results) == 1


async def test_network_down_is_reported_not_raised(gemini_settings, tmp_path: Path) -> None:
    with respx.mock() as router:
        router.route().mock(side_effect=httpx.ConnectError("offline"))
        results = await run_checks(cwd=tmp_path, settings_loader=gemini_settings)
    checks = _by_name(results)
    assert "can't reach Telegram" in checks["Telegram token"].detail
    assert "can't reach provider" in checks["LLM key (gemini)"].detail


async def test_config_problems_stop_early(tmp_path: Path) -> None:
    def broken():
        raise ConfigError(["TELEGRAM_BOT_TOKEN is missing", "ALLOWED_USER_IDS is missing"])

    results = await run_checks(cwd=tmp_path, settings_loader=broken)
    checks = _by_name(results)
    assert checks["Configuration"].status is Status.FAIL
    assert "TELEGRAM_BOT_TOKEN is missing" in checks["Configuration"].detail
    assert checks["Remaining checks"].status is Status.SKIP


async def test_synced_folders_flagged(make_settings, tmp_path: Path) -> None:
    synced = tmp_path / "OneDrive" / "Desktop"
    synced.mkdir(parents=True)
    with respx.mock() as router:
        _mock_telegram(router, webhook="https://example.com/hook")
        results = await run_checks(
            cwd=synced, settings_loader=lambda: make_settings(data_dir=synced / "data")
        )
    checks = _by_name(results)
    assert checks["Project folder"].status is Status.WARN
    assert checks["Data folder"].status is Status.FAIL
    assert checks["Telegram webhook"].status is Status.WARN
    assert checks["LLM key (fake)"].status is Status.WARN


async def test_database_revision_and_running_instance(settings, tmp_path: Path) -> None:
    settings.ensure_dirs()
    await upgrade_to_head_async(settings.resolved_database_url)
    with respx.mock() as router, InstanceLock(settings.lock_path):
        _mock_telegram(router)
        results = await run_checks(cwd=tmp_path, settings_loader=lambda: settings)
    checks = _by_name(results)
    assert "up to date" in checks["Database"].detail
    assert checks["Running instance"].status is Status.WARN


async def test_live_adzuna_check_uses_one_call(make_settings, tmp_path: Path) -> None:
    def loader():
        return make_settings(adzuna_app_id="id1", adzuna_app_key="key1", rapidapi_key="r")

    with respx.mock() as router:
        _mock_telegram(router)
        adzuna = router.get("https://api.adzuna.com/v1/api/jobs/in/search/1").respond(
            200, json={"results": []}
        )
        results = await run_checks(cwd=tmp_path, settings_loader=loader, live_sources=True)
    checks = _by_name(results)
    assert checks["Adzuna keys"].status is Status.OK
    assert adzuna.call_count == 1
    assert adzuna.calls.last.request.url.params["results_per_page"] == "1"
    assert checks["JSearch key"].status is Status.OK
