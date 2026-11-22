"""Local embeddings (ADR-006): fastembed + BAAI/bge-small-en-v1.5 on CPU via ONNX.

Free, offline after a one-time ~70 MB model download, and the resume never leaves the
laptop for stage-1 matching. If the model can't load (no internet on first run), we fall
back to the deterministic hashing embedder and log it, so matching still works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jobpilot.core.logging import get_logger
from jobpilot.llm.base import Embedder
from jobpilot.llm.fake import HashEmbedder

log = get_logger(__name__)


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self.name = model_name
        self.dimensions = 384
        self._cache_dir = cache_dir
        self._model: Any = None
        self._fallback: HashEmbedder | None = None
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._model is not None or self._fallback is not None:
            return
        async with self._lock:
            if self._model is not None or self._fallback is not None:
                return
            try:
                from fastembed import TextEmbedding  # noqa: PLC0415

                self._model = await asyncio.to_thread(
                    TextEmbedding, model_name=self.name, cache_dir=str(self._cache_dir)
                )
                log.info("embeddings.loaded", model=self.name)
            except Exception as exc:
                log.warning("embeddings.fallback_to_hashing", error=str(exc))
                self._fallback = HashEmbedder(dimensions=256)
                self.name = self._fallback.name
                self.dimensions = self._fallback.dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await self._load()
        if self._fallback is not None:
            return await self._fallback.embed(texts)
        vectors = await asyncio.to_thread(lambda: list(self._model.embed(texts, batch_size=32)))
        return [v.tolist() for v in vectors]

    async def warm_up(self) -> str:
        await self._load()
        return self.name


def build_embedder(use_local_model: bool, model_name: str, cache_dir: Path) -> Embedder:
    if use_local_model:
        return FastEmbedder(model_name, cache_dir)
    return HashEmbedder(dimensions=256)
