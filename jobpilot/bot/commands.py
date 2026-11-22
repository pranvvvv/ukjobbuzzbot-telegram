"""Single source of truth for bot commands: drives setMyCommands, /help and fallbacks.

Commands become visible in Telegram's menu only once their milestone ships, so the
owner never sees a command that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import BotCommand

CURRENT_MILESTONE = 9


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    milestone: int

    @property
    def available(self) -> bool:
        return self.milestone <= CURRENT_MILESTONE


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "Status and main menu", 1),
    CommandSpec("new", "Start fresh with a new resume", 2),
    CommandSpec("profile", "Show what I understood from your resume", 2),
    CommandSpec("preferences", "Review or edit your job preferences", 3),
    CommandSpec("search", "Run a search now", 4),
    CommandSpec("pause", "Pause the daily digest", 7),
    CommandSpec("resume", "Resume the daily digest", 7),
    CommandSpec("saved", "Jobs you saved", 6),
    CommandSpec("history", "Jobs I've already sent you", 6),
    CommandSpec("stats", "Runs, source quotas and AI cost", 7),
    CommandSpec("settime", "Change the daily digest time", 7),
    CommandSpec("deleteme", "Delete all your data", 3),
    CommandSpec("help", "What I can do", 1),
)
_BY_NAME = {c.name: c for c in COMMANDS}


def available_commands() -> list[CommandSpec]:
    return [c for c in COMMANDS if c.available]


def telegram_commands() -> list[BotCommand]:
    return [BotCommand(command=c.name, description=c.description) for c in available_commands()]


def lookup(name: str) -> CommandSpec | None:
    return _BY_NAME.get(name.lower())
