"""The encoder runs bge-m3's ONNX export through OpenVINO, without torch.

These tests hold the pipeline the model defines (the first token's hidden state,
then L2 normalisation), the order of the rows, and where the files come from:
`cache/models/<repo>/<revision>/`, nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import model_files
import numpy as np
import onnx_encoder
import pytest

ROOT = Path(__file__).resolve().parent.parent
PAD_ID = 1


class _Tokenizer:
    """One token per character code, so each text's tokens are known."""

    def encode_batch(self, texts):
        return [SimpleNamespace(ids=[ord(character) for character in text]) for text in texts]

    def token_to_id(self, token):
        return PAD_ID if token == onnx_encoder.PAD_TOKEN else None


class _CompiledModel:
    """A hidden state of (token id, position + 1) per position, like a compiled OpenVINO model."""

    def __init__(self) -> None:
        self.batches: list[dict] = []

    def output(self, _index):
        width = SimpleNamespace(get_length=lambda: 2)
        return SimpleNamespace(get_partial_shape=lambda: [None, None, width])

    def __call__(self, feeds):
        self.batches.append(feeds)
        ids = feeds["input_ids"].astype(np.float32)
        positions = np.broadcast_to(np.arange(1, ids.shape[1] + 1, dtype=np.float32), ids.shape)
        return [np.stack([ids, positions], axis=-1)]


def _expected(text: str) -> np.ndarray:
    vector = np.array([ord(text[0]), 1.0])
    return vector / np.linalg.norm(vector)


def test_rows_are_the_first_token_normalised_and_in_order(monkeypatch) -> None:
    monkeypatch.setattr(onnx_encoder, "BATCH_SIZE", 2)
    model = _CompiledModel()
    encoder = onnx_encoder.OnnxEncoder(model, _Tokenizer())
    texts = ["a", "a much longer text", "mid length", "zz"]

    vectors = encoder.encode(texts)

    assert vectors.dtype == np.float32
    assert np.allclose(vectors, np.stack([_expected(text) for text in texts]), atol=1e-6)
    assert [len(feeds["input_ids"]) for feeds in model.batches] == [2, 2]


def test_a_batch_is_padded_with_the_pad_token_and_masked() -> None:
    model = _CompiledModel()
    onnx_encoder.OnnxEncoder(model, _Tokenizer()).encode(["abc", "d"])

    feeds = model.batches[0]
    assert feeds["input_ids"].tolist() == [[97, 98, 99], [100, PAD_ID, PAD_ID]]
    assert feeds["attention_mask"].tolist() == [[1, 1, 1], [1, 0, 0]]


def test_no_text_gives_no_rows() -> None:
    encoder = onnx_encoder.OnnxEncoder(_CompiledModel(), _Tokenizer())

    assert encoder.encode([]).shape == (0, 2)


def test_a_file_not_installed_is_named(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_files, "models_root", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="onnx/model.onnx_data is not installed"):
        onnx_encoder.load_encoder("org/model", "f" * 40)


def test_the_loader_compiles_the_graph_from_the_pinned_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_files, "models_root", lambda: tmp_path)
    directory = model_files.model_directory("org/model", "f" * 40)
    for name in ("onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json"):
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"")
    compiled: list[Path] = []
    monkeypatch.setattr(
        onnx_encoder, "compiled_model", lambda path: compiled.append(path) or _CompiledModel()
    )
    monkeypatch.setattr(onnx_encoder, "_tokenizer", lambda path: _Tokenizer())

    encoder = onnx_encoder.load_encoder("org/model", "f" * 40)

    assert (compiled, encoder.dimension) == ([directory / "onnx/model.onnx"], 2)


def test_nothing_in_the_product_loads_a_model_through_torch_or_onnx_runtime() -> None:
    pattern = r"^\s*(from|import) (sentence_transformers|transformers|torch|onnxruntime)\b"
    offenders = [
        path.name
        for path in (ROOT / "scripts").glob("*.py")
        if re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    ]

    assert offenders == []


def test_the_search_extras_do_not_pull_torch() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    search = pyproject[pyproject.index("semantic = [") : pyproject.index("hybrid = [")]

    names = ("torch", "sentence-transformers", "optimum", "onnxruntime", "transformers")
    assert [name for name in names if name in search] == []
