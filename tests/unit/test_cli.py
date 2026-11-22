from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobpilot import __version__
from jobpilot.cli import app
from tests.conftest import TEST_TOKEN

runner = CliRunner()


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
    return tmp_path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_run_without_config_explains_and_exits_2(in_tmp: Path) -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "TELEGRAM_BOT_TOKEN is missing" in result.output


def test_doctor_without_config_fails_with_table(in_tmp: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Configuration" in result.output
    assert "FAIL" in result.output


def test_migrate_creates_database(in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DATA_DIR", str(in_tmp / "data"))
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "revision 0001" in result.output
    assert (in_tmp / "data" / "jobpilot.db").exists()
