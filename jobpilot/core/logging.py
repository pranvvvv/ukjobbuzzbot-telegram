"""Structured logging: JSON lines to a rotating file + readable console output.

Privacy (spec Section 11): logs never contain resume text, job descriptions, tokens,
API keys, or full emails/phone numbers. `redact` runs on every event, including
log records from third-party libraries (aiogram, httpx, apscheduler).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

# Keys whose values are dropped entirely, at any nesting depth.
SENSITIVE_KEYS = frozenset(
    {
        "resume_text",
        "raw_resume_text",
        "profile_json",
        "description",
        "job_description",
        "api_key",
        "llm_api_key",
        "app_key",
        "token",
        "bot_token",
        "password",
        "authorization",
        "cookie",
    }
)
REDACTED = "[redacted]"

_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
_URL_SECRET_RE = re.compile(
    r"(?P<k>[?&](?:app_key|app_id|api_key|apikey|key|token|access_token)=)[^&\s\"']+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Phone-ish: optional +, then 10-15 digits possibly separated by spaces/dashes.
_PHONE_RE = re.compile(r"(?<![\w:])\+?\d(?:[\s-]?\d){9,14}(?!\w)")


def mask_text(text: str) -> str:
    """Mask secrets and personal contact details inside free text."""
    text = _BOT_TOKEN_RE.sub("<telegram-token>", text)
    text = _URL_SECRET_RE.sub(lambda m: f"{m.group('k')}***", text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _PHONE_RE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(0))[-2:], text)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: drop sensitive keys, mask secrets/PII in every string."""
    for key in list(event_dict.keys()):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def _stdlib_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Render %-style args of foreign (stdlib) log records before redaction."""
    record = event_dict.get("_record")
    if record is not None and isinstance(event_dict.get("event"), str) and record.args:
        event_dict["event"] = record.getMessage()
        event_dict.pop("positional_args", None)
    return event_dict


_shared: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(
    level: str = "INFO", logs_dir: Path | None = None, *, json_console: bool | None = None
) -> None:
    """Configure structlog + stdlib logging once for the whole process."""
    if json_console is None:
        json_console = not sys.stderr.isatty()

    foreign_chain: list[Processor] = [*_shared, _stdlib_message]
    final_chain: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        redact,
        structlog.processors.format_exc_info,
    ]

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=foreign_chain,
            processors=[
                *final_chain,
                structlog.processors.JSONRenderer()
                if json_console
                else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ],
        )
    )
    handlers: list[logging.Handler] = [console]

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "jobpilot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=foreign_chain,
                processors=[*final_chain, structlog.processors.JSONRenderer()],
            )
        )
        handlers.append(file_handler)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    # Noisy libraries: keep their warnings, drop per-request chatter (httpx logs full URLs).
    for noisy in ("httpx", "httpcore", "aiosqlite", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[*_shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def bind_context(**values: Any) -> None:
    """Attach values (run_id, user_id...) to every log line in the current task."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "mask_text",
    "redact",
]
