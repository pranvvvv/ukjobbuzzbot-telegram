"""Typed configuration loaded from environment variables / `.env`.

Design rules:
- Secrets are `SecretStr`, so they never appear in reprs or logs.
- Startup fails fast with ONE message listing every problem (`load_settings`).
- Keys needed only by later features (job sources) are optional here; `doctor`
  reports them as warnings instead of blocking startup.
"""

from __future__ import annotations

import re
from datetime import time
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_data_dir
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from jobpilot.core.errors import ConfigError

APP_NAME = "JobPilot"
_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class LLMProvider(StrEnum):
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GROQ = "groq"
    FAKE = "fake"


def default_data_dir() -> Path:
    """Per-user app data dir, outside the repo (and outside OneDrive on Windows)."""
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # --- required -------------------------------------------------------
    telegram_bot_token: SecretStr
    allowed_user_ids: Annotated[frozenset[int], NoDecode] = Field(min_length=1)

    llm_provider: LLMProvider = LLMProvider.GEMINI
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None

    # --- job sources (optional until those milestones) --------------------
    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None
    rapidapi_key: SecretStr | None = None

    # --- sources & matching ---------------------------------------------------
    adzuna_country: str = "in"
    adzuna_daily_limit: int = Field(default=200, ge=0)
    adzuna_monthly_limit: int = Field(default=2000, ge=0)
    jsearch_monthly_limit: int = Field(default=100, ge=0)
    enable_remote_boards: bool = True
    companies_file: Path | None = None
    max_queries_per_run: int = Field(default=6, ge=1, le=12)
    match_threshold: int = Field(default=60, ge=0, le=100)
    # Stage-1-only ("basic", no AI) scores sit lower on the scale; calibrated with `eval`.
    basic_match_threshold: int = Field(default=40, ge=0, le=100)
    max_job_age_days: int = Field(default=14, ge=1, le=60)
    stage1_top_k: int = Field(default=30, ge=5, le=100)
    use_local_embeddings: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # --- cost guard -------------------------------------------------------------
    llm_max_tokens_per_run: int = Field(default=150_000, ge=1_000)
    llm_daily_budget_usd: float = Field(default=0.25, ge=0)

    # --- runtime ------------------------------------------------------------
    data_dir: Path = Field(default_factory=default_data_dir)
    database_url: str | None = None
    default_timezone: str = "Asia/Kolkata"
    default_digest_time: time = time(9, 0)
    default_jobs_per_digest: int = Field(default=10, ge=1, le=25)
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=20.0, gt=0)

    # ------------------------------------------------------------ validators
    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[,\s;]+", value) if p.strip()]
            if not parts:
                raise ValueError("is empty")
            for part in parts:
                if not part.lstrip("-").isdigit():
                    raise ValueError(f"'{part}' is not a numeric Telegram user ID")
            return frozenset(int(p) for p in parts)
        if isinstance(value, int):
            return frozenset({value})
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def _check_token_shape(cls, value: SecretStr) -> SecretStr:
        if not _BOT_TOKEN_RE.match(value.get_secret_value().strip()):
            raise ValueError("doesn't look like a BotFather token (expected '<digits>:<letters>')")
        return SecretStr(value.get_secret_value().strip())

    @field_validator("default_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone '{value}' (use an IANA name like Asia/Kolkata)"
            ) from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("must be one of DEBUG, INFO, WARNING, ERROR")
        return upper

    @field_validator(
        "llm_api_key",
        "adzuna_app_id",
        "adzuna_app_key",
        "rapidapi_key",
        "llm_model",
        "companies_file",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("data_dir", "database_url", mode="before")
    @classmethod
    def _blank_is_default(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return default_data_dir() if info.field_name == "data_dir" else None
        return value

    @model_validator(mode="after")
    def _llm_key_required(self) -> Settings:
        if self.llm_provider is not LLMProvider.FAKE and self.llm_api_key is None:
            raise ValueError(f"LLM_API_KEY is required when LLM_PROVIDER={self.llm_provider.value}")
        return self

    # ------------------------------------------------------------ derived
    @cached_property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'jobpilot.db').as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "jobpilot.lock"

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.logs_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)


def _describe(error: dict[str, Any]) -> str:
    field = ".".join(str(p) for p in error.get("loc", ())) or "configuration"
    env_name = field.upper()
    if error.get("type") == "missing":
        return f"{env_name} is missing"
    message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
    if field == "configuration":
        return message
    if message.startswith("is "):
        return f"{env_name} {message}"
    return f"{env_name}: {message}"


def load_settings(**overrides: Any) -> Settings:
    """Load settings or raise ConfigError listing *every* problem in plain language."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = [_describe(dict(e)) for e in exc.errors()]
        problems = [
            f"{p} (send your bot /start, then run: python -m jobpilot whoami --save)"
            if p.startswith("ALLOWED_USER_IDS")
            else p
            for p in problems
        ]
        raise ConfigError(problems) from None


def is_under_sync_folder(path: Path) -> bool:
    """True if `path` sits in a OneDrive/Dropbox/iCloud/Google Drive synced folder."""
    markers = ("onedrive", "dropbox", "icloud", "google drive", "googledrive")
    lowered = [part.lower() for part in path.resolve().parts]
    return any(marker in part for part in lowered for marker in markers)
