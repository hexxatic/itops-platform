"""The local embedding model is loaded once per process, however many apps start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fastembed
import pytest

from itops.core import ai


class FakeModel:
    loads = 0

    def __init__(self, model: str, cache_dir: str) -> None:
        FakeModel.loads += 1

    @staticmethod
    def list_supported_models() -> list[dict[str, Any]]:
        return [{"model": "example/model", "dim": 4}]

    def query_embed(self, texts: list[str]) -> Any:
        import numpy as np

        return iter([np.zeros(4) for _ in texts])


async def test_embedders_share_one_loaded_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fastembed, "TextEmbedding", FakeModel)
    monkeypatch.setattr(ai, "_MODELS", {})
    first = ai.FastEmbedder("example/model", str(tmp_path))
    second = ai.FastEmbedder("example/model", str(tmp_path))
    assert await first.embed_query("a") == [0.0] * 4
    assert await second.embed_query("b") == [0.0] * 4
    assert FakeModel.loads == 1
