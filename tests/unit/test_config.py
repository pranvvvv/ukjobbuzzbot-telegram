from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from jobpilot.core.config import LLMProvider, is_under_sync_folder, load_settings
from jobpilot.core.errors import ConfigError
from tests.conftest import TEST_TOKEN


def test_minimal_valid_settings(settings, tmp_path: Path) -> None:
    assert settings.allowed_user_ids == frozenset({111})
    assert settings.llm_provider is LLMProvider.FAKE
    assert settings.default_timezone == "Asia/Kolkata"
    assert settings.default_digest_time == time(9, 0)
    assert settings.resolved_database_url.endswith("/data/jobpilot.db")
    assert settings.resolved_database_url.startswith("sqlite+aiosqlite:///")


def test_secrets_never_appear_in_repr(settings) -> None:
    assert TEST_TOKEN not in repr(settings)
    assert TEST_TOKEN not in str(settings.model_dump())


def test_missing_everything_lists_every_problem_at_once() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(_env_file=None)
    problems = info.value.problems
    assert "TELEGRAM_BOT_TOKEN is missing" in problems
    assert any(p.startswith("ALLOWED_USER_IDS is missing") and "whoami" in p for p in problems)
    assert info.value.exit_code == 2
    assert "python -m jobpilot doctor" in str(info.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("111", {111}), ("111, 222", {111, 222}), ("111 222;333", {111, 222, 333})],
)
def test_allowed_ids_accept_common_separators(make_settings, raw, expected) -> None:
    assert make_settings(allowed_user_ids=raw).allowed_user_ids == frozenset(expected)


def test_non_numeric_user_id_is_explained(make_settings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(allowed_user_ids="111,@pranav")
    assert any("'@pranav' is not a numeric Telegram user ID" in p for p in info.value.problems)


def test_empty_allowlist_rejected(make_settings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(allowed_user_ids="")
    assert any(p.startswith("ALLOWED_USER_IDS is empty") for p in info.value.problems)
    assert not any("Frozenset" in p for p in info.value.problems)


def test_bad_token_shape(make_settings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(telegram_bot_token="not-a-token")
    assert any("BotFather" in p for p in info.value.problems)


def test_unknown_timezone(make_settings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(default_timezone="India/Hyderabad")
    assert any("unknown timezone" in p for p in info.value.problems)


def test_real_provider_requires_key(make_settings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(llm_provider="gemini", llm_api_key="")
    assert any("LLM_API_KEY is required" in p for p in info.value.problems)


def test_blank_optional_values_become_none(make_settings) -> None:
    s = make_settings(adzuna_app_id="  ", llm_model="", database_url="", data_dir="")
    assert s.adzuna_app_id is None
    assert s.llm_model is None
    assert s.database_url is None
    assert s.data_dir.name == "JobPilot"


def test_reads_dotenv_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"TELEGRAM_BOT_TOKEN={TEST_TOKEN}\nALLOWED_USER_IDS=5,6\nLLM_PROVIDER=fake\n",
        encoding="utf-8",
    )
    s = load_settings(_env_file=env, data_dir=tmp_path)
    assert s.allowed_user_ids == frozenset({5, 6})


@pytest.mark.parametrize(
    ("path", "synced"),
    [
        (Path("/home/x/OneDrive/Desktop/JobPilot"), True),
        (Path("/home/x/OneDrive - College/stuff"), True),
        (Path("/home/x/Dropbox/code"), True),
        (Path("/home/x/dev/jobpilot"), False),
    ],
)
def test_sync_folder_detection(path: Path, synced: bool) -> None:
    assert is_under_sync_folder(path) is synced
