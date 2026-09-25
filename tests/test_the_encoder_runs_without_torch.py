"""The encoder runs through ONNX Runtime, without torch.

Measured on 2026-09-25 on 402 texts from the vault: the same vectors as the
`sentence-transformers` path to 1.4e-07, a load of 1.6 s instead of 6.5 s, and a
faster batch. These tests hold the pipeline the model defines (mean pooling over
the attention mask, then L2 normalisation), the order of the rows, and where the
files come from. See docs/research/2026-09-25-the-encoder-runs-without-torch.md.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import numpy as np
import onnx_encoder
import pytest

ROOT = Path(__file__).resolve().parent.parent
PAD_ID = 1


class _Encoding:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids


class _Tokenizer:
    """One token per character code, so each text's tokens are known."""

    def encode_batch(self, texts):
        return [_Encoding([ord(character) for character in text]) for text in texts]

    def token_to_id(self, token):
        return PAD_ID


class _Input:
    def __init__(self, name: str) -> None:
        self.name = name


class _Output:
    shape = ["batch", "sequence", 2]


class _Session:
    """A hidden state of (token id, 1) per position; padding reads as (1, 1)."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    def get_inputs(self):
        return [_Input("input_ids"), _Input("attention_mask"), _Input("token_type_ids")]

    def get_outputs(self):
        return [_Output()]

    def run(self, _names, feeds):
        ids = feeds["input_ids"].astype(np.float32)
        self.batches.append(len(ids))
        return [np.stack([ids, np.ones_like(ids)], axis=-1)]


def _expected(text: str) -> np.ndarray:
    vector = np.array([np.mean([ord(character) for character in text]), 1.0])
    return vector / np.linalg.norm(vector)


def test_rows_are_mean_pooled_over_real_tokens_normalised_and_in_order(monkeypatch) -> None:
    monkeypatch.setattr(onnx_encoder, "BATCH_SIZE", 2)
    session = _Session()
    encoder = onnx_encoder.OnnxEncoder(session, _Tokenizer())
    texts = ["a", "a much longer text", "mid length", "zz"]

    vectors = encoder.encode(texts)

    assert vectors.dtype == np.float32
    assert np.allclose(vectors, np.stack([_expected(text) for text in texts]), atol=1e-6)
    assert session.batches == [2, 2]


def test_no_text_gives_no_rows() -> None:
    encoder = onnx_encoder.OnnxEncoder(_Session(), _Tokenizer())

    assert encoder.encode([]).shape == (0, 2)


def _hub(found: dict[str, str]) -> types.ModuleType:
    hub = types.ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = lambda repo_id, filename, *, revision: found.get(filename)
    return hub


def test_a_file_not_in_the_cache_is_named(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", _hub({}))

    with pytest.raises(FileNotFoundError, match="onnx/model.onnx is not in the local cache"):
        onnx_encoder.cached_file("org/model", onnx_encoder.MODEL_FILE, "f" * 40)


def test_the_loader_reads_the_pinned_files_on_the_cpu(monkeypatch) -> None:
    opened: list[tuple[str, list[str]]] = []
    runtime = types.ModuleType("onnxruntime")

    def session(path, providers):
        opened.append((path, providers))
        return _Session()

    runtime.InferenceSession = session
    found = {onnx_encoder.MODEL_FILE: "/cache/model.onnx", onnx_encoder.TOKENIZER_FILE: "/cache/tokenizer.json"}
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "huggingface_hub", _hub(found))
    monkeypatch.setattr(onnx_encoder, "_tokenizer", lambda path: _Tokenizer())

    encoder = onnx_encoder.load_encoder("org/model", "f" * 40)

    # The loader passes the path as the platform spells it; Windows writes `\\`.
    assert (opened, encoder.dimension) == ([(str(Path("/cache/model.onnx")), ["CPUExecutionProvider"])], 2)


def test_nothing_in_the_product_loads_the_encoder_through_sentence_transformers() -> None:
    offenders = [
        path.name
        for path in (ROOT / "scripts").glob("*.py")
        if re.search(r"^\s*(from|import) sentence_transformers", path.read_text(encoding="utf-8"), re.M)
    ]

    assert offenders == []


def test_the_search_extras_do_not_pull_torch() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    semantic = pyproject[pyproject.index("semantic = [") : pyproject.index("hybrid = [")]

    assert [name for name in ("torch", "sentence-transformers", "optimum") if name in semantic] == []
