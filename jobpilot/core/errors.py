"""Application error hierarchy.

Every error a user can see carries a message written for a human, not a stack trace.
Exit codes are stable so scripts (autostart, Docker healthchecks) can react to them.
"""

from __future__ import annotations


class JobPilotError(Exception):
    """Base class for all expected, user-facing failures."""

    exit_code: int = 1


class ConfigError(JobPilotError):
    """Configuration is missing or invalid. Lists every problem at once (fail fast, fix once)."""

    exit_code = 2

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        bullet_list = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            "JobPilot can't start because of configuration problems:\n"
            f"{bullet_list}\n"
            "Edit your .env file (see .env.example), then run: python -m jobpilot doctor"
        )


class AlreadyRunningError(JobPilotError):
    """Another JobPilot instance holds the lock or is polling the same bot token."""

    exit_code = 3


class TelegramConflictDetectedError(AlreadyRunningError):
    """Telegram returned 409 Conflict: another process is polling with this bot token."""

    exit_code = 4
