"""Test doubles: a fake Telegram Bot API at the HTTP-session layer.

The real aiogram dispatcher, filters, middlewares and response parsing all run; only
the network is replaced. Error responses go through aiogram's own `check_response`,
so a scripted 409 becomes a genuine `TelegramConflictError`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetFile,
    GetMe,
    GetUpdates,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    MessageEntity,
    Update,
    User,
)

BOT_USER: dict[str, Any] = {
    "id": 123456,
    "is_bot": True,
    "first_name": "JobPilot",
    "username": "jobpilot_test_bot",
}
Reply = tuple[int, dict[str, Any]]


def ok(result: Any) -> Reply:
    return 200, {"ok": True, "result": result}


def error(code: int, description: str) -> Reply:
    return code, {"ok": False, "error_code": code, "description": description}


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.handlers: dict[type, Callable[[Any], Reply]] = {}
        self.files: dict[str, bytes] = {}  # file_id -> content, served via getFile + download
        self._message_id = 1000

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.requests.append(method)
        handler = self.handlers.get(type(method))
        status, payload = handler(method) if handler else self._default(method)
        response = self.check_response(bot, method, status, json.dumps(payload))
        return response.result

    def _default(self, method: TelegramMethod[Any]) -> Reply:
        if isinstance(method, GetMe):
            return ok(BOT_USER)
        if isinstance(method, GetUpdates):
            return ok([])
        if isinstance(method, GetFile):
            data = self.files.get(method.file_id, b"")
            return ok(
                {
                    "file_id": method.file_id,
                    "file_unique_id": "u",
                    "file_size": len(data),
                    "file_path": f"documents/{method.file_id}",
                }
            )
        if isinstance(method, SendMessage | EditMessageText):
            self._message_id += 1
            return ok(
                {
                    "message_id": self._message_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": method.chat_id or 1, "type": "private"},
                    "text": method.text,
                }
            )
        return ok(True)

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - signature fixed by aiogram's BaseSession
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        file_id = url.rsplit("/", 1)[-1]
        yield self.files.get(file_id, b"")

    # ------------------------------------------------------------ helpers
    def of_type[M](self, kind: type[M]) -> list[M]:
        return [r for r in self.requests if isinstance(r, kind)]

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.of_type(SendMessage)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return self.of_type(AnswerCallbackQuery)


_update_ids = iter(range(1, 1_000_000))


def tg_user(user_id: int, first_name: str = "Pranav") -> User:
    return User(id=user_id, is_bot=False, first_name=first_name)


def message_update(user_id: int, text: str | None = None, **extra: Any) -> Update:
    entities = None
    if text and text.startswith("/"):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    message = Message(
        message_id=next(_update_ids),
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=tg_user(user_id),
        text=text,
        entities=entities,
        **extra,
    )
    return Update(update_id=next(_update_ids), message=message)


def callback_update(user_id: int, data: str) -> Update:
    message = Message(
        message_id=next(_update_ids),
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User.model_validate(BOT_USER),
        text="menu",
    )
    query = CallbackQuery(
        id=str(next(_update_ids)),
        from_user=tg_user(user_id),
        chat_instance="test",
        data=data,
        message=message,
    )
    return Update(update_id=next(_update_ids), callback_query=query)


# ------------------------------------------------------------------ jobs & AI fakes
import re as _re  # noqa: E402
from datetime import date as _date  # noqa: E402

from jobpilot.matching.normalize import JobPosting  # noqa: E402
from jobpilot.resume.profile import heuristic_extract  # noqa: E402


class StubSource:
    """In-memory job source."""

    credit_url: str | None = None

    def __init__(
        self,
        name: str,
        jobs: list[JobPosting] | None = None,
        fail: Exception | None = None,
        label: str | None = None,
    ) -> None:
        self.name = name
        self.label = label or name.title()
        self.jobs = jobs or []
        self.fail = fail
        self.calls = 0

    async def search(self, queries: list[Any]) -> list[JobPosting]:
        self.calls += 1
        if self.fail:
            raise self.fail
        return [j.model_copy() for j in self.jobs]


def resume_check_responder(prompt: Any) -> dict[str, Any]:
    is_resume = bool(_re.search(r"education|experience|skills", prompt.user, _re.I))
    return {"is_resume": is_resume, "reason": "has resume sections" if is_resume else "a recipe"}


def extraction_responder(prompt: Any) -> dict[str, Any]:
    """Behaves like a decent model: parse the fenced resume with the rule-based parser."""
    m = _re.search(r"<resume>\n(.*)\n</resume>", prompt.user, _re.S)
    text = m.group(1) if m else prompt.user
    data = heuristic_extract(text, _date(2026, 11, 21)).model_dump()
    data["gaps_or_flags"] = []
    data["domains"] = ["AI automation"] if "bot" in text.lower() else []
    return data


def scores_responder(score: int = 32) -> Callable[[Any], dict[str, Any]]:
    def respond(prompt: Any) -> dict[str, Any]:
        ids = _re.findall(r'<job id="(j\d+)">', prompt.user)
        return {
            "scores": [
                {
                    "job_id": jid,
                    "skills_match": score,
                    "role_fit": 17,
                    "seniority_fit": 13,
                    "location_mode_fit": 9,
                    "preference_fit": 8,
                    "why_fit": ["FastAPI + PostgreSQL match your WhatsApp-bot project"],
                    "missing_skills": ["Kubernetes"],
                    "red_flags": [],
                    "one_line_pitch": "Lead with your production automation work.",
                }
                for jid in ids
            ]
        }

    return respond


def queries_responder(prompt: Any) -> dict[str, Any]:
    return {
        "queries": [
            {"keywords": "backend developer python", "location": "Hyderabad", "remote": False},
            {"keywords": "AI engineer", "location": None, "remote": True},
        ]
    }
