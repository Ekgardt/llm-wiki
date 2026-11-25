"""Encode text with the pinned bge-m3 ONNX export through OpenVINO on the CPU.

bge-m3's dense vector is the hidden state of its first token (CLS), L2
normalised, with no instruction prefix on either side, truncating at 512 tokens
in this product (the model reads up to 8192; longer chunks cost quadratically
and the chunker keeps pages far shorter). Measured on 400 chunks of this vault:
OpenVINO encoded them 2.5x faster than ONNX Runtime with the same threads, and
its vectors agree with ONNX Runtime's at cosine 0.9997.

Files are read from `cache/models/<repo>/<revision>/` only;
`scripts/install_models.py` is the one place that fetches them. A file that is
not installed raises `FileNotFoundError`, which the search reports as
`model_unavailable`.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from embedding_model import EMBEDDING_GRAPH_FILE, EMBEDDING_TOKENIZER_FILE, EMBEDDING_WEIGHTS_FILE
from model_files import installed_file
from openvino_runtime import compiled_model

# The product's truncation: the reranker's window too, and the length both models
# were measured at on this vault.
MAX_TOKENS = 512
# Texts are sorted by token count and each batch is padded to its own longest
# member: a larger batch pads more and gains nothing on a CPU (4 was fastest of
# 4, 8, 16 and 32 on four cores).
BATCH_SIZE = 4
PAD_TOKEN = "<pad>"
_MIN_NORM = 1e-12


def _tokenizer(path: Any) -> Any:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(path))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.no_padding()
    return tokenizer


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


def _output_width(model: Any) -> int:
    return int(model.output(0).get_partial_shape()[-1].get_length())


class OnnxEncoder:
    """`encode(texts)` gives one L2-normalised float32 row per text, in order."""

    def __init__(self, model: Any, tokenizer: Any) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._pad_id = tokenizer.token_to_id(PAD_TOKEN)
        self.dimension = _output_width(model)

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
        hidden = self._model({"input_ids": ids, "attention_mask": mask})[0]
        return _normalized(hidden[:, 0])


def load_encoder(repo_id: str, revision: str) -> OnnxEncoder:
    """The pinned model from `cache/models/`, compiled for the CPU."""
    installed_file(repo_id, revision, EMBEDDING_WEIGHTS_FILE)
    tokenizer = _tokenizer(installed_file(repo_id, revision, EMBEDDING_TOKENIZER_FILE))
    return OnnxEncoder(compiled_model(installed_file(repo_id, revision, EMBEDDING_GRAPH_FILE)),
                       tokenizer)
