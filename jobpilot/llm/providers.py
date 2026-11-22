"""Provider adapters (ADR-007). Each implements one request; `BaseLLM` does the rest.

- Claude:  Messages API, `output_config.format` = JSON schema (no beta header needed)
- OpenAI:  Chat Completions, `response_format` = strict JSON schema
- Groq:    the OpenAI SDK pointed at Groq's OpenAI-compatible endpoint (strict on gpt-oss)
- Gemini:  google-genai, `response_mime_type=application/json` + `response_json_schema`

SDK-level retries handle 429/5xx with backoff; anything still failing becomes
`LLMUnavailableError`, which callers turn into the "basic match" fallback.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobpilot.core.config import LLMProvider, Settings
from jobpilot.llm.base import BaseLLM, LLMClient, LLMUnavailableError, Usage

DEFAULT_MODELS: dict[LLMProvider, str] = {
    LLMProvider.ANTHROPIC: "claude-haiku-5-5",
    LLMProvider.GEMINI: "gemini-2.5-flash-lite",
    LLMProvider.OPENAI: "gpt-5-mini",
    LLMProvider.GROQ: "openai/gpt-oss-120b",
}
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class AnthropicLLM(BaseLLM):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout: float = 60.0) -> None:
        from anthropic import AsyncAnthropic  # noqa: PLC0415  (anthropic 2.x ships httpx2)

        super().__init__(model)
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=3)

    async def _call(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, Usage]:
        import anthropic  # noqa: PLC0415

        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError as exc:
            raise LLMUnavailableError(f"anthropic: {type(exc).__name__}: {exc}") from exc
        # Only text blocks carry the JSON (thinking/tool blocks may also be present).
        text = "".join(
            str(getattr(b, "text", "")) for b in resp.content if getattr(b, "type", "") == "text"
        )
        return text, Usage(resp.usage.input_tokens, resp.usage.output_tokens)

    async def aclose(self) -> None:
        await self._client.close()


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI, and Groq through its OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider: str = "openai",
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        from openai import AsyncOpenAI  # noqa: PLC0415  (openai 3.x ships its own httpx2)

        super().__init__(model)
        self.provider = provider
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=3
        )

    async def _call(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, Usage]:
        import openai  # noqa: PLC0415

        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
                max_completion_tokens=max_tokens,
            )
        except openai.APIError as exc:
            raise LLMUnavailableError(f"{self.provider}: {type(exc).__name__}: {exc}") from exc
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return text, Usage(
            usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0
        )

    async def aclose(self) -> None:
        await self._client.close()


class GeminiLLM(BaseLLM):
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        from google import genai  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        super().__init__(model)
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout * 1000), httpx_async_client=http_client
            ),
        )

    async def _call(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, Usage]:
        from google.genai import errors, types  # noqa: PLC0415

        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    max_output_tokens=max_tokens,
                    temperature=0.2,
                ),
            )
        except errors.APIError as exc:
            raise LLMUnavailableError(f"gemini: {exc}") from exc
        meta = resp.usage_metadata
        return resp.text or "", Usage(
            (meta.prompt_token_count or 0) if meta else 0,
            (meta.candidates_token_count or 0) if meta else 0,
        )


def build_llm(settings: Settings) -> LLMClient:
    """Construct the configured provider. `fake` gives an offline client for development."""
    provider = settings.llm_provider
    if provider is LLMProvider.FAKE or settings.llm_api_key is None:
        from jobpilot.llm.fake import OfflineLLM  # noqa: PLC0415

        return OfflineLLM()
    key = settings.llm_api_key.get_secret_value()
    model = settings.llm_model or DEFAULT_MODELS[provider]
    if provider is LLMProvider.ANTHROPIC:
        return AnthropicLLM(key, model)
    if provider is LLMProvider.GEMINI:
        return GeminiLLM(key, model)
    if provider is LLMProvider.GROQ:
        return OpenAICompatibleLLM(key, model, provider="groq", base_url=GROQ_BASE_URL)
    return OpenAICompatibleLLM(key, model, provider="openai")
