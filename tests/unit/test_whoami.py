from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jobpilot.cli import app
from jobpilot.core.errors import ConfigError, TelegramConflictDetectedError
from jobpilot.whoami import fetch_senders, load_token, save_allowed_ids
from tests.conftest import TEST_TOKEN

UPDATES = f"https://api.telegram.org/bot{TEST_TOKEN}/getUpdates"
GETME = f"https://api.telegram.org/bot{TEST_TOKEN}/getMe"
runner = CliRunner()


def _getme(router: respx.Router) -> None:
    router.get(GETME).respond(
        200, json={"ok": True, "result": {"id": 1, "username": "jp_test_bot"}}
    )


def _msg(uid: int, name: str, is_bot: bool = False) -> dict[str, Any]:
    return {
        "update_id": uid,
        "message": {
            "from": {"id": uid, "is_bot": is_bot, "first_name": name, "username": name.lower()}
        },
    }


async def test_collects_unique_human_senders_without_acknowledging() -> None:
    with respx.mock() as router:
        route = router.get(UPDATES).respond(
            200,
            json={
                "ok": True,
                "result": [_msg(7, "Pranav"), _msg(7, "Pranav"), _msg(9, "Bot", True)],
            },
        )
        async with httpx.AsyncClient() as client:
            senders = await fetch_senders(TEST_TOKEN, client)
    assert [(s.id, s.username) for s in senders] == [(7, "pranav")]
    params = route.calls.last.request.url.params
    assert params["timeout"] == "0"
    assert "offset" not in params


@pytest.mark.parametrize(
    ("status", "exc"), [(401, ConfigError), (409, TelegramConflictDetectedError)]
)
async def test_errors_are_explained(status: int, exc: type[Exception]) -> None:
    with respx.mock() as router:
        router.get(UPDATES).respond(status, json={"ok": False})
        async with httpx.AsyncClient() as client:
            with pytest.raises(exc):
                await fetch_senders(TEST_TOKEN, client)


def test_save_replaces_only_the_allowlist_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nTELEGRAM_BOT_TOKEN=abc\nALLOWED_USER_IDS=\nLLM_PROVIDER=fake\n",
        encoding="utf-8",
    )
    save_allowed_ids(env, [42])
    assert env.read_text(encoding="utf-8") == (
        "# comment\nTELEGRAM_BOT_TOKEN=abc\nALLOWED_USER_IDS=42\nLLM_PROVIDER=fake\n"
    )


def test_save_appends_when_line_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc", encoding="utf-8")
    save_allowed_ids(env, [1, 2])
    assert env.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=abc\nALLOWED_USER_IDS=1,2\n"


def test_load_token_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_token(tmp_path / "nope.env")


def test_cli_whoami_save_writes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TEST_TOKEN}\nALLOWED_USER_IDS=\n", encoding="utf-8"
    )
    with respx.mock() as router:
        _getme(router)
        router.get(UPDATES).respond(200, json={"ok": True, "result": [_msg(5551234, "Pranav")]})
        result = runner.invoke(app, ["whoami", "--save"])
    assert result.exit_code == 0, result.output
    assert "5551234" in result.output
    assert "ALLOWED_USER_IDS=5551234" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_cli_whoami_no_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
    with respx.mock() as router:
        _getme(router)
        router.get(UPDATES).respond(200, json={"ok": True, "result": []})
        result = runner.invoke(app, ["whoami", "--wait", "0"])
    assert result.exit_code == 1
    assert "@jp_test_bot" in result.output
    assert "No message arrived" in result.output


def test_cli_whoami_waits_for_a_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter pressed before /start was sent: first check empty, the long poll catches it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
    with respx.mock() as router:
        _getme(router)
        route = router.get(UPDATES).mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": []}),
                httpx.Response(200, json={"ok": True, "result": [_msg(77, "Pranav")]}),
            ]
        )
        result = runner.invoke(app, ["whoami", "--save", "--wait", "30"])
    assert result.exit_code == 0, result.output
    assert "t.me/jp_test_bot" in result.output
    assert route.calls[0].request.url.params["timeout"] == "0"
    assert route.calls[1].request.url.params["timeout"] == "25"
    assert "offset" not in route.calls[1].request.url.params
    assert "ALLOWED_USER_IDS=77" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_cli_whoami_rejected_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
    with respx.mock() as router:
        router.get(GETME).respond(401, json={"ok": False})
        result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 2
    assert "rejected by Telegram" in result.output
