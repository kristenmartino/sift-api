from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from services.embedder import BATCH_SIZE, embed_texts

ZERO_VECTOR = [0.0] * 512


def _fake_result(n: int):
    """Stub of a voyageai embed() result with n real 512-dim vectors."""
    result = MagicMock()
    result.embeddings = [[0.1] * 512 for _ in range(n)]
    return result


class TestEmbedTexts:
    def test_empty_input_returns_empty(self):
        assert asyncio.run(embed_texts([])) == []

    def test_successful_embed_returns_real_vectors(self):
        texts = ["a", "b", "c"]
        with patch("services.embedder.voyageai.Client") as Client:
            Client.return_value.embed.return_value = _fake_result(len(texts))
            out = asyncio.run(embed_texts(texts))
        assert len(out) == len(texts)
        assert all(v is not None and len(v) == 512 for v in out)
        assert ZERO_VECTOR not in out

    def test_failed_batch_returns_none_not_zero_vectors(self):
        texts = ["a", "b", "c"]
        with patch("services.embedder.voyageai.Client") as Client:
            Client.return_value.embed.side_effect = RuntimeError("voyage down")
            out = asyncio.run(embed_texts(texts))
        # Same length, all None — the failure mode is NULL, never a zero vector.
        assert len(out) == len(texts)
        assert out == [None, None, None]
        assert ZERO_VECTOR not in out

    def test_multi_batch_preserves_alignment_on_partial_failure(self):
        # Two batches: the first (128) succeeds, the second (3) fails. The output
        # must stay the same length with failed positions as None — proving the
        # article/vector alignment the pipeline relies on is preserved.
        texts = [f"t{i}" for i in range(BATCH_SIZE + 3)]

        def embed_side_effect(batch, **kwargs):
            if "t0" in batch:  # first batch
                return _fake_result(len(batch))
            raise RuntimeError("voyage down on second batch")

        with patch("services.embedder.voyageai.Client") as Client:
            Client.return_value.embed.side_effect = embed_side_effect
            out = asyncio.run(embed_texts(texts))

        assert len(out) == len(texts)
        # First batch embedded for real...
        assert all(v is not None and len(v) == 512 for v in out[:BATCH_SIZE])
        # ...second (failed) batch is None, never zero vectors.
        assert out[BATCH_SIZE:] == [None, None, None]
        assert ZERO_VECTOR not in out
