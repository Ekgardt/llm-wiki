"""Encode text with the pinned E5 model through ONNX Runtime, without torch.

The model's own repository ships `onnx/model.onnx` at the pinned revision, and
its sentence pipeline is Transformer, mean pooling over the attention mask, then
L2 normalisation, truncating at 512 tokens. This module does exactly that with
`onnxruntime` and `tokenizers`. Measured on 2026-09-25 against the
`sentence-transformers` path it replaces, on 402 texts from the vault: the same
vectors to 1.4e-07, a load of 1.6 s instead of 6.5 s (neither `torch` nor
`sentence-transformers` is imported), and 4.8 s instead of 5.9 s to encode them
all. See `docs/research/2026-09-25-the-encoder-runs-without-torch.md`.

Files come from the local Hugging Face cache only; `scripts/install_models.py`
is the one place that fetches them. A file that is not cached raises
`FileNotFoundError`, which the search reports as `model_unavailable`.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

MODEL_FILE = "onnx/model.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 512
# Texts are sorted by token count and each batch is padded to its own longest
# member. On four cores 4 was fastest (4.8 s for 402 texts; 8: 5.3 s, 16: 5.4 s,
# 32: 7.5 s): a larger batch pads more and gains nothing on a CPU.
BATCH_SIZE = 4
PAD_TOKEN = "<pad>"
# The smallest mask sum and vector norm divided by, as the libraries it replaces
# clamp them, so an empty input gives zeros rather than NaN.
_MIN_TOKENS = 1e-9
_MIN_NORM = 1e-12


def cached_file(repo_id: str, filename: str, revision: str) -> Path:
    """The cached copy of one pinned file; never the network."""
    from huggingface_hub import try_to_load_from_cache

    found = try_to_load_from_cache(repo_id, filename, revision=revision)
    if not isinstance(found, str):
        raise FileNotFoundError(
            f"{repo_id}@{revision[:12]}: {filename} is not in the local cache"
        )
    return Path(found)


def _tokenizer(path: Path) -> Any:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(path))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.no_padding()
    return tokenizer


def _mean_pooled(hidden: Any, mask: Any) -> Any:
    import numpy as np

    weights = mask[..., None].astype(hidden.dtype)
    summed = (hidden * weights).sum(axis=1)
    counts = np.clip(weights.sum(axis=1), _MIN_TOKENS, None)
    return summed / counts


def _normalized(vectors: Any) -> Any:
    import numpy as np

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, _MIN_NORM, None)


def _padded(rows: list[list[int]], pad_id: int) -> tuple[Any, Any]:
    """Ids padded to the longest row, and the mask that marks the real tokens."""
    import numpy as np

    width = max(len(row) for row in rows)
    ids = np.full((len(rows), width), pad_id, dtype=np.int64)
    mask = np.zeros((len(rows), width), dtype=np.int64)
    for position, row in enumerate(rows):
        ids[position, : len(row)] = row
        mask[position, : len(row)] = 1
    return ids, mask


class OnnxEncoder:
    """`encode(texts)` gives one L2-normalised float32 row per text, in order."""

    def __init__(self, session: Any, tokenizer: Any) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._pad_id = tokenizer.token_to_id(PAD_TOKEN)
        self._input_names = frozenset(item.name for item in session.get_inputs())
        self.dimension = int(session.get_outputs()[0].shape[-1])

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np

        token_ids = [encoding.ids for encoding in self._tokenizer.encode_batch(list(texts))]
        vectors = np.zeros((len(token_ids), self.dimension), dtype=np.float32)
        order = sorted(range(len(token_ids)), key=lambda index: len(token_ids[index]), reverse=True)
        for start in range(0, len(order), BATCH_SIZE):
            chunk = order[start : start + BATCH_SIZE]
            vectors[chunk] = self._batch([token_ids[index] for index in chunk])
        return vectors

    def _batch(self, rows: list[list[int]]) -> Any:
        ids, mask = _padded(rows, self._pad_id)
        hidden = self._session.run(None, self._feeds(ids, mask))[0]
        return _normalized(_mean_pooled(hidden, mask))

    def _feeds(self, ids: Any, mask: Any) -> dict[str, Any]:
        import numpy as np

        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        return feeds


def load_encoder(repo_id: str, revision: str) -> OnnxEncoder:
    """The pinned model from the local cache, on the CPU."""
    import onnxruntime

    model = cached_file(repo_id, MODEL_FILE, revision)
    tokenizer = _tokenizer(cached_file(repo_id, TOKENIZER_FILE, revision))
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return OnnxEncoder(session, tokenizer)
