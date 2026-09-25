"""The one place that names the embedding model and its prefixes.

The search runtime and the generation builder encode text with it, and each
used to carry its own copy of the name. A copy that drifts is worse than no copy
at all here: queries embedded by one model and pages by another still produce
numbers, and the numbers are meaningless.

The model is multilingual on purpose. The pages of this vault are written in
English and its owner asks in Russian, and an English-only encoder scored every
candidate alike (0.43 to 0.52 on three real questions) while this one put the
right passage first every time. The lexical leg cannot cross languages at all,
so the dense leg is the only one that can.
"""
from __future__ import annotations

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
# The ONNX export the repository ships at that revision, which
# `scripts/onnx_encoder.py` reads and `scripts/install_models.py` verifies after
# a download; the Hub cache names the blob by this digest too. See
# `docs/research/2026-09-25-the-encoder-runs-without-torch.md`.
EMBEDDING_WEIGHTS_FILE = "onnx/model.onnx"
EMBEDDING_WEIGHTS_SHA256 = "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
EMBEDDING_WEIGHTS_BYTES = 470268510
# Everything the encoder reads, and so everything the installer fetches.
EMBEDDING_READ_FILES = (EMBEDDING_WEIGHTS_FILE, "tokenizer.json")
# The PyTorch weights the encoder read through `sentence-transformers` until
# 2026-09-25; the installer removes them once the ONNX weights are verified.
EMBEDDING_RETIRED_FILES = ("model.safetensors",)
EMBEDDING_DIM = 384

# E5 is trained with these two prefixes and loses accuracy without them, so both
# sides carry one: questions are queries, pages are passages.
QUERY_INSTRUCTION = "query:"
PASSAGE_INSTRUCTION = "passage:"


def embedding_instruction(is_query: bool) -> str:
    if is_query:
        return QUERY_INSTRUCTION
    return PASSAGE_INSTRUCTION


def prefixed_texts(texts: list[str], is_query: bool) -> list[str]:
    """Apply the prefix this model expects for that side of the comparison."""
    instruction = embedding_instruction(is_query)
    if not instruction:
        return list(texts)
    return [f"{instruction} {text}" for text in texts]
