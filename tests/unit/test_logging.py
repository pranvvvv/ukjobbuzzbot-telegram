from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from jobpilot.core.logging import REDACTED, configure_logging, get_logger, mask_text, redact
from tests.conftest import TEST_TOKEN


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        ("mail pranav.k@example.com now", "pranav.k@", "p***@example.com"),
        ("call +91 98765 43210 today", "98765", "***10"),
        ("call 9876543210", "9876543210", "***10"),
        (f"token {TEST_TOKEN}", TEST_TOKEN, "<telegram-token>"),
        ("GET /search?app_id=abc123&app_key=s3cr3t&what=python", "s3cr3t", "app_key=***"),
        ("Authorization: Bearer sk-abcdefghijklmnop", "sk-abcdefghijklmnop", "Bearer ***"),
    ],
)
def test_mask_text(raw: str, must_not_contain: str, must_contain: str) -> None:
    masked = mask_text(raw)
    assert must_not_contain not in masked
    assert must_contain in masked


def test_mask_text_keeps_ordinary_numbers() -> None:
    assert mask_text("scored 87 of 100 in 2026") == "scored 87 of 100 in 2026"
    assert mask_text("user_id=1234567") == "user_id=1234567"


def test_redact_drops_sensitive_keys_at_any_depth() -> None:
    event = {
        "event": "profile.parsed",
        "resume_text": "John Doe, john@x.com",
        "nested": {"description": "job text", "ok": "fine", "items": [{"token": "t"}]},
    }
    out = redact(None, "info", event)
    assert out["resume_text"] == REDACTED
    assert out["nested"]["description"] == REDACTED
    assert out["nested"]["items"][0]["token"] == REDACTED
    assert out["nested"]["ok"] == "fine"


def test_file_log_is_json_and_redacted(tmp_path: Path) -> None:
    configure_logging("INFO", tmp_path, json_console=True)
    try:
        get_logger("t").info("resume.received", resume_text="secret resume", user_id=1)
        logging.getLogger("aiogram.event").info("bot token is %s", TEST_TOKEN)
        for handler in logging.getLogger().handlers:
            handler.flush()
        lines = (tmp_path / "jobpilot.log").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        by_event = {r["event"]: r for r in records}
        assert by_event["resume.received"]["resume_text"] == REDACTED
        assert by_event["resume.received"]["level"] == "info"
        assert "timestamp" in by_event["resume.received"]
        foreign = next(r for r in records if r["event"].startswith("bot token"))
        assert TEST_TOKEN not in foreign["event"]
        assert foreign["logger"] == "aiogram.event"
    finally:
        structlog.reset_defaults()
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
