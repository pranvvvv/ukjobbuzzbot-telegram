"""`python -m jobpilot whoami`: find your Telegram user ID from messages sent to your bot.

Setup chicken-and-egg: the bot ignores everyone until ALLOWED_USER_IDS is set, but you
need your ID to set it. This needs only the bot token: message your bot, run whoami,
and (with --save) your ID is written into .env.

It reads pending updates with getUpdates(timeout=0) and no offset, which does not
acknowledge them, so nothing is lost for the bot itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobpilot.core.errors import ConfigError, TelegramConflictDetectedError

_ALLOWED_LINE = re.compile(r"(?m)^ALLOWED_USER_IDS=.*$")


class _TokenOnly(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    telegram_bot_token: SecretStr


@dataclass(frozen=True, slots=True)
class Sender:
    id: int
    first_name: str | None
    username: str | None


def load_token(env_file: Path | None = Path(".env")) -> str:
    try:
        return _TokenOnly(_env_file=env_file).telegram_bot_token.get_secret_value().strip()
    except ValidationError:
        raise ConfigError(["TELEGRAM_BOT_TOKEN is missing"]) from None


def _senders_from(updates: list[dict[str, Any]]) -> list[Sender]:
    found: dict[int, Sender] = {}
    for update in updates:
        for kind in ("message", "edited_message", "callback_query", "my_chat_member"):
            sender = (update.get(kind) or {}).get("from")
            if sender and not sender.get("is_bot"):
                found[sender["id"]] = Sender(
                    sender["id"], sender.get("first_name"), sender.get("username")
                )
    return list(found.values())


def _check_token_status(resp: httpx.Response) -> None:
    if resp.status_code in (401, 404):
        raise ConfigError(["TELEGRAM_BOT_TOKEN was rejected by Telegram (revoked or mistyped)"])


async def fetch_bot_username(token: str, client: httpx.AsyncClient) -> str:
    """Which bot this token belongs to, so you message the right one."""
    resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    _check_token_status(resp)
    resp.raise_for_status()
    return str(resp.json().get("result", {}).get("username", "?"))


async def fetch_senders(
    token: str, client: httpx.AsyncClient, *, wait_seconds: int = 0
) -> list[Sender]:
    """Senders of pending updates. With wait_seconds > 0, long-polls until one arrives.

    No offset is ever sent, so nothing is acknowledged or lost.
    """
    resp = await client.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"timeout": wait_seconds, "limit": 100},
    )
    _check_token_status(resp)
    if resp.status_code == 409:
        raise TelegramConflictDetectedError(
            "Telegram won't hand out messages right now: JobPilot (or a webhook) is already "
            "receiving them. Stop the running bot, then run whoami again."
        )
    resp.raise_for_status()
    return _senders_from(resp.json().get("result", []))


def save_allowed_ids(env_path: Path, ids: list[int]) -> None:
    """Set ALLOWED_USER_IDS in .env, keeping every other line exactly as it was."""
    line = "ALLOWED_USER_IDS=" + ",".join(str(i) for i in ids)
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if _ALLOWED_LINE.search(text):
        text = _ALLOWED_LINE.sub(line, text, count=1)
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    env_path.write_text(text, encoding="utf-8", newline="\n")
