from __future__ import annotations

import math

import pytest
from pydantic import BaseModel

from jobpilot.llm.base import Embedder, LLMClient, LLMOutputError, Prompt
from jobpilot.llm.fake import FakeLLM, HashEmbedder


class Verdict(BaseModel):
    is_resume: bool
    confidence: float


PROMPT = Prompt(system="classify", user="<resume>text</resume>", name="classify@v1")


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeLLM(), LLMClient)
    assert isinstance(HashEmbedder(), Embedder)


async def test_validated_retry_recovers_from_bad_json() -> None:
    llm = FakeLLM({"Verdict": ['{"is_resume": tru', {"is_resume": True, "confidence": 0.9}]})
    result = await llm.complete_json(Verdict, PROMPT)
    assert result.value == Verdict(is_resume=True, confidence=0.9)
    assert result.attempts == 2
    assert result.usage.input_tokens > 0
    assert len(llm.calls) == 2


async def test_gives_up_after_max_attempts() -> None:
    llm = FakeLLM({"Verdict": ["nope", "still nope", "never"]}, max_attempts=3)
    with pytest.raises(LLMOutputError, match="invalid output after retries"):
        await llm.complete_json(Verdict, PROMPT)
    assert len(llm.calls) == 3


async def test_missing_script_is_an_error() -> None:
    with pytest.raises(LLMOutputError, match="no scripted response"):
        await FakeLLM().complete_json(Verdict, PROMPT)


async def test_callable_responder_sees_prompt() -> None:
    llm = FakeLLM({"Verdict": lambda p: {"is_resume": "resume" in p.user, "confidence": 1.0}})
    assert (await llm.complete_json(Verdict, PROMPT)).value.is_resume is True


async def test_hash_embedder_is_deterministic_and_normalised() -> None:
    emb = HashEmbedder(dimensions=32)
    a, b, c = await emb.embed(["python fastapi", "python fastapi", "sales manager"])
    assert a == b
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)
    assert a != c
