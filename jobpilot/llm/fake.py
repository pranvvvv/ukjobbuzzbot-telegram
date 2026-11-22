"""Deterministic fake LLM for tests and offline development (LLM_PROVIDER=fake)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from jobpilot.llm.base import LLMOutputError, LLMResult, Prompt, Usage, current_meter

Responder = Callable[[Prompt], str | dict[str, Any]]


class FakeLLM:
    """Returns scripted responses per schema name and records every call.

    Responses may be raw JSON strings (to test repair paths) or dicts. A list of
    responses for one schema is consumed in order, so tests can script
    "bad JSON, then good JSON" to exercise the validated-retry loop.
    """

    provider = "fake"

    def __init__(
        self,
        responses: dict[str, list[str | dict[str, Any]] | Responder] | None = None,
        *,
        max_attempts: int = 3,
        model: str = "fake-1",
    ) -> None:
        self.model = model
        self._responses = dict(responses or {})
        self._max_attempts = max_attempts
        self.calls: list[tuple[str, Prompt]] = []

    async def complete_json[T: BaseModel](
        self, schema: type[T], prompt: Prompt, *, max_output_tokens: int = 2048
    ) -> LLMResult[T]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self.calls.append((schema.__name__, prompt))
            raw = self._next(schema.__name__, prompt)
            text = raw if isinstance(raw, str) else json.dumps(raw)
            usage = Usage(len(prompt.system + prompt.user) // 4, len(text) // 4)
            meter = current_meter()
            if meter is not None:
                meter.check()
                meter.add(usage)
            try:
                return LLMResult(schema.model_validate_json(text), usage, self.model, attempt)
            except ValidationError as exc:
                last_error = exc
        raise LLMOutputError(f"{schema.__name__}: invalid output after retries: {last_error}")

    def _next(self, key: str, prompt: Prompt) -> str | dict[str, Any]:
        entry = self._responses.get(key)
        if entry is None:
            raise LLMOutputError(f"FakeLLM has no scripted response for {key}")
        if callable(entry):
            return entry(prompt)
        if not entry:
            raise LLMOutputError(f"FakeLLM ran out of scripted responses for {key}")
        return entry.pop(0) if len(entry) > 1 else entry[0]

    async def aclose(self) -> None:
        return None


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder: stable vectors for tests, no model download."""

    name = "hash-embedder"

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in text.lower().split():
            digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
            vec[int.from_bytes(digest, "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OfflineLLM:
    """Used when no LLM key is configured: every call reports 'unavailable'.

    Callers already handle that path (rule-based resume parsing, stage-1-only
    "basic match" scoring), so JobPilot still works end to end without a key.
    """

    provider = "offline"
    model = "none"

    async def complete_json[T: BaseModel](
        self, schema: type[T], prompt: Prompt, *, max_output_tokens: int = 2048
    ) -> LLMResult[T]:
        from jobpilot.llm.base import LLMUnavailableError  # noqa: PLC0415

        raise LLMUnavailableError("no LLM configured (set LLM_PROVIDER and LLM_API_KEY)")

    async def aclose(self) -> None:
        return None
