from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.bot.app import build_bot, build_dispatcher
from jobpilot.container import Container, build_container
from jobpilot.core.config import Settings, load_settings
from jobpilot.db.engine import create_engine, create_session_factory
from jobpilot.db.migrate import upgrade_to_head_async
from jobpilot.llm.fake import FakeLLM, HashEmbedder
from tests.fakes import FakeTelegram

OWNER_ID = 111
STRANGER_ID = 999
TEST_TOKEN = "123456:AAH-test-token-abcdefghijklmnopqrstuvwxyz"

_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_USER_IDS",
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_MODEL",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "RAPIDAPI_KEY",
    "DATA_DIR",
    "DATABASE_URL",
    "DEFAULT_TIMEZONE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the developer's real environment or .env file."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "_env_file": None,
            "telegram_bot_token": TEST_TOKEN,
            "allowed_user_ids": str(OWNER_ID),
            "llm_provider": "fake",
            "data_dir": tmp_path / "data",
            "enable_remote_boards": False,
            "use_local_embeddings": False,
        }
        values.update(overrides)
        return load_settings(**values)

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
async def sessions(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings.ensure_dirs()
    await upgrade_to_head_async(settings.resolved_database_url)
    engine = create_engine(settings.resolved_database_url)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def bot(settings: Settings, telegram: FakeTelegram) -> Bot:
    return build_bot(settings, telegram)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM({})


@pytest.fixture
async def container(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], fake_llm: FakeLLM
) -> AsyncIterator[Container]:
    c = build_container(
        settings, sessions, llm=fake_llm, embedder=HashEmbedder(), http_client=httpx.AsyncClient()
    )
    c.delivery.sender._interval = 0  # no per-chat pacing in tests
    yield c
    await c.aclose()


@pytest.fixture
def dp(container: Container) -> Dispatcher:
    dispatcher = build_dispatcher(container)
    dispatcher["paste_debounce"] = 0.01
    return dispatcher
