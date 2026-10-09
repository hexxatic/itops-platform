"""AI services (design: "AI: three tiers").

* Tier 1, bring-your-own AI over MCP, lives in :mod:`itops.core.mcp`.
* Tier 2, **local embeddings**: a small open-source model run in-process with ONNX
  (``fastembed``). Nothing leaves the network, no per-use cost. Powers semantic search.
  The model downloads once into ``ITOPS_AI_MODELS_DIR`` and then runs offline; if it
  can't load, callers get :class:`AIUnavailable` and search stays keyword-only.
* Tier 3, embedded LLM: :meth:`AIService.llm` returns ``None`` unless a provider is
  configured *and* the specific feature is enabled. Modules must work when it's ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from itops.core.config import Settings

log = logging.getLogger("itops.ai")


class AIUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class LLM(Protocol):
    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int = 1024
    ) -> str: ...


class FastEmbedder:
    """Local ONNX embedding model. Loaded lazily on first use, run on a worker thread
    (it's CPU work) and one batch at a time."""

    def __init__(self, model: str, cache_dir: str) -> None:
        from fastembed import TextEmbedding

        info = next((m for m in TextEmbedding.list_supported_models() if m["model"] == model), None)
        if info is None:
            raise AIUnavailable(f"unknown embedding model {model!r}")
        self.model = model
        self.dim = int(info["dim"])
        self._cache_dir = cache_dir
        self._impl: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        if self._impl is None:
            from fastembed import TextEmbedding

            Path(self._cache_dir).mkdir(parents=True, exist_ok=True)
            try:
                self._impl = TextEmbedding(self.model, cache_dir=self._cache_dir)
            except Exception as exc:  # no network on first run, corrupt cache, ...
                raise AIUnavailable(
                    f"embedding model {self.model!r} failed to load: {exc}"
                ) from exc
        return self._impl

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:
            return await asyncio.to_thread(
                lambda: [v.tolist() for v in self._load().passage_embed(list(texts), batch_size=32)]
            )

    async def embed_query(self, text: str) -> list[float]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: next(iter(self._load().query_embed([text]))).tolist()
            )


class AIService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._embedder: Embedder | None = None
        if settings.ai_embeddings_provider == "fastembed":
            try:
                self._embedder = FastEmbedder(settings.ai_embeddings_model, settings.ai_models_dir)
            except (AIUnavailable, ImportError) as exc:
                log.warning("local embeddings disabled: %s", exc)
        self._llm: LLM | None = None  # tier 3 provider: off by default

    @property
    def embeddings_available(self) -> bool:
        return self._embedder is not None

    @property
    def embedding_model(self) -> str | None:
        return self._embedder.model if self._embedder else None

    @property
    def embedding_dim(self) -> int | None:
        return self._embedder.dim if self._embedder else None

    def _need(self) -> Embedder:
        if self._embedder is None:
            raise AIUnavailable("no embeddings provider configured (ITOPS_AI_EMBEDDINGS_PROVIDER)")
        return self._embedder

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed content (documents, passages) for storage."""
        return await self._need().embed_passages(texts)

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search question (some models encode questions differently)."""
        return await self._need().embed_query(text)

    def llm(self, feature: str) -> LLM | None:
        """The LLM for ``feature`` (``"<module>.<feature>"``), or ``None`` if not enabled."""
        if self._llm is None or feature not in self._settings.ai_llm_features:
            return None
        return self._llm
