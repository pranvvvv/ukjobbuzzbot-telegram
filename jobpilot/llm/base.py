"""Provider-agnostic LLM and embedding interfaces (ADR-006, ADR-008).

`LLMClient` does structured JSON completions only. Embeddings are a separate
`Embedder` (Groq and Anthropic have no embedding API; stage-1 matching runs locally).

`BaseLLM` implements the shared contract once, for every provider:
- schema-constrained output (provider-native JSON schema),
- Pydantic validation with a repair loop that feeds the validation error back,
- token accounting into the current run's `UsageMeter` (cost guard, ADR-015).
"""

from __future__ import annotations

import abc
import contextvars
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from jobpilot.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Prompt:
    """A rendered prompt. `system` holds instructions; `user` holds delimited, untrusted data."""

    system: str
    user: str
    name: str = "unnamed"  # prompt file id + version, recorded for traceability


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResult[T: BaseModel]:
    value: T
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    attempts: int = 1


class LLMError(Exception):
    """Base for LLM failures."""


class LLMUnavailableError(LLMError):
    """Provider down, quota exhausted, or auth failure, after retries."""


class LLMOutputError(LLMError):
    """The model kept returning output that fails schema validation."""


class LLMBudgetExceededError(LLMUnavailableError):
    """The run or daily token/cost budget is spent: callers fall back to basic scoring."""


# ------------------------------------------------------------------ usage meter
@dataclass
class UsageMeter:
    """Accumulates tokens for one run and enforces its token budget."""

    max_tokens: int | None = None
    usage: Usage = field(default_factory=Usage)
    calls: int = 0

    def check(self) -> None:
        if self.max_tokens is not None and self.usage.total >= self.max_tokens:
            raise LLMBudgetExceededError(
                f"token budget reached ({self.usage.total}/{self.max_tokens})"
            )

    def add(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        self.calls += 1


_current_meter: contextvars.ContextVar[UsageMeter | None] = contextvars.ContextVar(
    "jobpilot_usage_meter", default=None
)


def set_meter(meter: UsageMeter | None) -> contextvars.Token[UsageMeter | None]:
    return _current_meter.set(meter)


def reset_meter(token: contextvars.Token[UsageMeter | None]) -> None:
    _current_meter.reset(token)


def current_meter() -> UsageMeter | None:
    return _current_meter.get()


# ------------------------------------------------------------------ JSON schema
def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → strict-mode schema accepted by OpenAI, Groq, Claude and Gemini.

    Inlines `$ref`s, marks every property required, forbids extra keys, drops titles
    and defaults. Our schemas are written strict-compatible: optional = `X | None`.
    """
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties":  # property NAMES are data (a field may be called "title")
                out[key] = {name: walk(sub) for name, sub in value.items()}
            elif key not in {"title", "default"}:  # schema metadata
                out[key] = walk(value)
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        return out

    result: dict[str, Any] = walk(raw)
    return result


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


# ------------------------------------------------------------------ protocols
@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    async def complete_json[T: BaseModel](
        self, schema: type[T], prompt: Prompt, *, max_output_tokens: int = 2048
    ) -> LLMResult[T]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Embedder(Protocol):
    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class BaseLLM(abc.ABC):
    """Shared validate-and-repair loop. Adapters implement only `_call`."""

    provider: str = "base"

    def __init__(self, model: str, *, max_repairs: int = 2) -> None:
        self.model = model
        self._max_repairs = max_repairs

    @abc.abstractmethod
    async def _call(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, Usage]:
        """One provider request returning (raw JSON text, usage). Raise LLMUnavailableError."""

    async def complete_json[T: BaseModel](
        self, schema: type[T], prompt: Prompt, *, max_output_tokens: int = 2048
    ) -> LLMResult[T]:
        json_schema = strict_json_schema(schema)
        meter = current_meter()
        total = Usage()
        user = prompt.user
        last_error = ""
        for attempt in range(1, self._max_repairs + 2):
            if meter is not None:
                meter.check()
            text, usage = await self._call(
                prompt.system, user, schema.__name__, json_schema, max_output_tokens
            )
            total = total + usage
            if meter is not None:
                meter.add(usage)
            try:
                value = schema.model_validate_json(_strip_code_fences(text))
            except ValidationError as exc:
                last_error = _short_errors(exc)
                log.warning(
                    "llm.invalid_output",
                    prompt=prompt.name,
                    attempt=attempt,
                    errors=last_error,
                )
                user = (
                    f"{prompt.user}\n\nYour previous reply did not match the required JSON "
                    f"schema. Errors: {last_error}\nReply again with corrected JSON only."
                )
                continue
            log.info(
                "llm.ok",
                prompt=prompt.name,
                model=self.model,
                attempts=attempt,
                tokens_in=total.input_tokens,
                tokens_out=total.output_tokens,
            )
            return LLMResult(value, total, self.model, attempt)
        raise LLMOutputError(f"{schema.__name__}: invalid output after retries: {last_error}")

    async def aclose(self) -> None:
        return None


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg')}")
    return "; ".join(parts)


def dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
