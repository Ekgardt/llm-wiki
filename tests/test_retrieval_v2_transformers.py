"""The retrieval v2 model loaders use pinned native assets and normalized fusion."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import CORPUS, all_normalized

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_matrix  # noqa: E402
import retrieval_v2_transformers  # noqa: E402


def _scored(candidates, scores):
    return [
        retrieval_v2_contract.ScoredCandidate(candidate, score)
        for candidate, score in zip(candidates, scores)
    ]


def _rescaled(items, factor: float):
    return [retrieval_v2_contract.ScoredCandidate(item.candidate, item.score * factor) for item in items]


def _ranking_identity(items):
    return [(item.evidence_id, item.score) for item in items]


def test_embeddinggemma_loader_uses_pinned_native_sentence_transformer(tmp_path, monkeypatch):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="google/embeddinggemma-300m",
        variant_id="float32-128d",
    )
    calls = []

    class Tokenizer:
        padding_side = None
        truncation_side = None

    class Model:
        tokenizer = Tokenizer()
        max_seq_length = None

        def encode(self, texts, **options):
            calls.append((list(texts), options))
            return [[1.0] * 768 for _text in texts]

    def sentence_transformer(model_id, **options):
        calls.append((model_id, options))
        return Model()

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        type("SentenceTransformers", (), {"SentenceTransformer": sentence_transformer}),
    )

    encoder = retrieval_v2_transformers.load_transformer_embedding(
        selection,
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )
    encoded = encoder(
        ["task: search result | query: cache"],
        batch_size=8,
        max_length=512,
        pooling="mean",
        padding_side="right",
        truncation_side="right",
    )

    assert calls[0] == (
        "google/embeddinggemma-300m",
        {
            "cache_folder": str(tmp_path),
            "local_files_only": True,
            "model_kwargs": {"torch_dtype": "float32"},
            "revision": "57c266a740f537b4dc058e1b0cda161fd15afa75",
            "trust_remote_code": False,
        },
    )
    assert calls[1][1] == {
        "batch_size": 8,
        "convert_to_numpy": True,
        "normalize_embeddings": False,
        "show_progress_bar": False,
    }
    assert len(encoded) == 1
    assert len(encoded[0]) == 768


def test_bge_m3_loader_requires_complete_sparse_linear_state_without_torch(
    tmp_path, monkeypatch
):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
    )
    asset = tmp_path / "sparse_linear.pt"
    asset.write_bytes(b"pinned sparse state")
    monkeypatch.setattr(retrieval_v2_transformers, "BGE_M3_SPARSE_LINEAR_SHA256", retrieval_v2_environment.sha256_file(asset))
    calls = []

    class Tensor:
        def __init__(self, shape):
            self.shape = shape

    state = {"weight": Tensor((1, 1024)), "bias": Tensor((1,))}

    class SparseLinear:
        def __init__(self, in_features, out_features, *, bias):
            calls.append(("linear", in_features, out_features, bias))

        def load_state_dict(self, loaded_state, *, strict):
            calls.append(("state", loaded_state, strict))

        def eval(self):
            calls.append(("sparse-eval",))
            return self

    class Torch:
        float32 = "float32"
        nn = type("NN", (), {"Linear": SparseLinear})

        @staticmethod
        def load(path, *, map_location, weights_only):
            calls.append(("load", path, map_location, weights_only))
            return state

    class Tokenizer:
        padding_side = "right"
        truncation_side = "right"

    class Model:
        def eval(self):
            calls.append(("model-eval",))
            return self

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(("tokenizer", args, kwargs))
            return Tokenizer()

    class AutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(("model", args, kwargs))
            return Model()

    monkeypatch.setitem(sys.modules, "torch", Torch())
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        type("Transformers", (), {"AutoModel": AutoModel, "AutoTokenizer": AutoTokenizer}),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type("Hub", (), {"hf_hub_download": staticmethod(lambda **_options: str(asset))}),
    )

    encoder = retrieval_v2_transformers.load_transformer_embedding(
        selection,
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )

    assert callable(encoder)
    assert ("load", asset.resolve(), "cpu", True) in calls
    assert ("linear", 1024, 1, True) in calls
    assert ("state", state, True) in calls


def test_bge_m3_loader_uses_pinned_native_sparse_linear_asset(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
    )
    asset = tmp_path / "sparse_linear.pt"
    weight = torch.zeros((1, 1024), dtype=torch.float32)
    weight[0, 0] = 1.0
    bias = torch.tensor([0.1], dtype=torch.float32)
    torch.save({"weight": weight, "bias": bias}, asset)
    monkeypatch.setattr(retrieval_v2_transformers, "BGE_M3_SPARSE_LINEAR_SHA256", retrieval_v2_environment.sha256_file(asset))
    calls = []

    class Tokenizer:
        padding_side = "right"
        truncation_side = "right"
        cls_token_id = 0
        eos_token_id = 1
        pad_token_id = 2
        unk_token_id = 3

        def __call__(self, texts, **options):
            calls.append((list(texts), options))
            return {
                "input_ids": torch.tensor([[0, 42, 7], [0, 42, 8]][: len(texts)]),
                "attention_mask": torch.ones((len(texts), 3), dtype=torch.long),
            }

    class Model:
        def eval(self):
            return self

        def __call__(self, **batch):
            hidden = torch.zeros((len(batch["input_ids"]), 3, 1024), dtype=torch.float32)
            hidden[:, 0, 0] = 1.0
            hidden[:, 1, 0] = 0.5
            hidden[:, 2, 0] = 0.25
            return type("Output", (), {"last_hidden_state": hidden})()

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            return Tokenizer()

    class AutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            return Model()

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        type("Transformers", (), {"AutoModel": AutoModel, "AutoTokenizer": AutoTokenizer}),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type(
            "Hub",
            (),
            {
                "hf_hub_download": staticmethod(
                    lambda **options: calls.append(("asset", options)) or str(asset)
                )
            },
        ),
    )

    encoder = retrieval_v2_transformers.load_transformer_embedding(
        selection,
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )
    encoded = encoder(
        ["first", "second"],
        batch_size=8,
        max_length=512,
        pooling="cls",
        padding_side="right",
        truncation_side="right",
    )

    assert encoded["dense_vecs"].shape == (2, 1024)
    assert encoded["lexical_weights"] == [
        {"42": pytest.approx(0.6), "7": pytest.approx(0.35)},
        {"42": pytest.approx(0.6), "8": pytest.approx(0.35)},
    ]
    asset_call = next(options for marker, options in calls if marker == "asset")
    assert asset_call == {
        "cache_dir": str(tmp_path),
        "filename": "sparse_linear.pt",
        "local_files_only": True,
        "repo_id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
    }


def test_qwen_longest_first_preserves_fixed_prefix_suffix_and_balances_pair():
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_text(encoding="utf-8"))
    formatting = next(
        item["formatting"]
        for item in matrix["rerankers"]
        if item["id"] == "Qwen/Qwen3-Reranker-0.6B"
    )

    class Tokenizer:
        def __init__(self):
            self.ids = {}

        def __call__(self, text, add_special_tokens=False):
            assert add_special_tokens is False
            tokens = re.findall(r"<\|[^|]+\|>|\w+|[^\w\s]", text)
            return {"input_ids": [self.ids.setdefault(token, len(self.ids) + 1) for token in tokens]}

    tokenizer = Tokenizer()
    query = " ".join(f"query{i}" for i in range(800))
    document = " ".join(f"document{i}" for i in range(900))
    built = retrieval_v2_transformers.qwen_reranker_input_ids(
        tokenizer, formatting, query, document, formatting["max_length_tokens"]
    )
    suffix_ids = tokenizer(formatting["assistant_suffix"], add_special_tokens=False)["input_ids"]
    prefix_ids = tokenizer(formatting["system_prefix"], add_special_tokens=False)["input_ids"]

    assert len(built["input_ids"]) == formatting["max_length_tokens"]
    assert built["input_ids"][: len(prefix_ids)] == prefix_ids
    assert built["input_ids"][-len(suffix_ids) :] == suffix_ids
    assert abs(built["query_tokens_kept"] - built["document_tokens_kept"]) <= 1
    assert built["query_tokens_kept"] < 800
    assert built["document_tokens_kept"] < 900


def test_rrf_confidence_is_scale_invariant_and_normalized():
    candidates = [
        retrieval_v2_contract.Candidate(str(index), str(index), f"synthetic/{index}", "EN", "p", "active", "2025-01-01", None, (), str(index))
        for index in range(3)
    ]
    first = _scored(candidates, (9, 6, 3))
    second = _scored(list(reversed(candidates)), (900, 600, 300))
    fused = retrieval_v2_transformers.fuse_rankings((first, second), candidates, limit=3)
    scaled = retrieval_v2_transformers.fuse_rankings(
        (_rescaled(first, 1000), _rescaled(second, 0.001)),
        candidates,
        limit=3,
    )

    assert all_normalized([item.score for item in fused])
    assert _ranking_identity(fused) == _ranking_identity(scaled)
