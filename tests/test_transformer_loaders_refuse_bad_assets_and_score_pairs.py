from __future__ import annotations

import importlib
import math
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.retrieval_v2_helpers import BENCHMARK, CORPUS

loaders = importlib.import_module("retrieval_v2_transformers")
matrix_module = importlib.import_module("retrieval_v2_matrix")
environment_module = importlib.import_module("retrieval_v2_environment")

MATRIX_PATH = BENCHMARK / "model-matrix-v1.json"


@pytest.fixture
def torch_module() -> Any:
    return pytest.importorskip("torch")


def _selection(model_id: str, variant_id: str, reranker_id: str | None = None) -> Any:
    return matrix_module.load_model_selection(
        MATRIX_PATH, CORPUS, model_id=model_id, variant_id=variant_id, reranker_id=reranker_id
    )


def _qwen_formatting() -> dict[str, Any]:
    selection = _selection("BAAI/bge-small-en-v1.5", "float32-384d", "Qwen/Qwen3-Reranker-0.6B")
    formatting: dict[str, Any] = dict(selection.reranker["formatting"])
    return formatting


class WordTokenizer:
    pad_token_id: int | None = None
    eos_token_id = 0

    def __init__(self) -> None:
        self.ids: dict[str, int] = {}

    def __call__(self, text: str, add_special_tokens: bool = True) -> dict[str, list[int]]:
        tokens = re.findall(r"<\|[^|]+\|>|\w+|[^\w\s]", text)
        return {"input_ids": [self.ids.setdefault(token, len(self.ids) + 1) for token in tokens]}


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attributes: object) -> None:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_an_embedder_that_asks_for_remote_code_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="remote model code is forbidden"):
        loaders.load_transformer_embedding(
            _selection("BAAI/bge-small-en-v1.5", "float32-384d"),
            cache_root=tmp_path,
            local_files_only=True,
            trust_remote_code=True,
        )


@pytest.mark.parametrize(
    ("missing_module", "model_id", "variant_id"),
    [
        ("sentence_transformers", "google/embeddinggemma-300m", "float32-128d"),
        ("transformers", "BAAI/bge-small-en-v1.5", "float32-384d"),
    ],
)
def test_an_embedder_without_its_native_library_names_the_missing_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_module: str,
    model_id: str,
    variant_id: str,
) -> None:
    monkeypatch.setitem(sys.modules, missing_module, None)

    with pytest.raises(ValueError, match="requires the retrieval-benchmark extra"):
        loaders.load_transformer_embedding(
            _selection(model_id, variant_id),
            cache_root=tmp_path,
            local_files_only=True,
            trust_remote_code=False,
        )


def test_a_sentence_transformer_refuses_a_pooling_its_model_does_not_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sentence_transformer(model_id: str, **options: object) -> SimpleNamespace:
        return SimpleNamespace(tokenizer=SimpleNamespace(), max_seq_length=None)

    _install_module(monkeypatch, "sentence_transformers", SentenceTransformer=sentence_transformer)
    encoder = loaders.load_transformer_embedding(
        _selection("google/embeddinggemma-300m", "float32-128d"),
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )

    with pytest.raises(ValueError, match="unsupported native pooling: cls"):
        encoder(
            ["text"],
            batch_size=1,
            max_length=8,
            pooling="cls",
            padding_side="right",
            truncation_side="right",
        )


def _install_hub(monkeypatch: pytest.MonkeyPatch, asset: Path) -> None:
    def download(**options: object) -> str:
        return str(asset)

    _install_module(monkeypatch, "huggingface_hub", hf_hub_download=download)


def test_a_sparse_asset_outside_the_model_cache_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    outside = tmp_path / "sparse_linear.pt"
    outside.write_bytes(b"planted")
    _install_hub(monkeypatch, outside)

    with pytest.raises(ValueError, match="escaped the model cache"):
        loaders._verified_sparse_asset({"id": "BAAI/bge-m3", "revision": "r"}, cache_root, True)


def test_a_sparse_asset_with_another_digest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "sparse_linear.pt"
    asset.write_bytes(b"not the pinned asset")
    _install_hub(monkeypatch, asset)

    with pytest.raises(ValueError, match="SHA256 mismatch"):
        loaders._verified_sparse_asset({"id": "BAAI/bge-m3", "revision": "r"}, tmp_path, True)


def test_a_sparse_asset_inside_the_cache_with_the_pinned_digest_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "snapshots" / "sparse_linear.pt"
    asset.parent.mkdir()
    asset.write_bytes(b"pinned")
    _install_hub(monkeypatch, asset)
    monkeypatch.setattr(
        loaders, "BGE_M3_SPARSE_LINEAR_SHA256", environment_module.sha256_file(asset)
    )

    resolved = loaders._verified_sparse_asset(
        {"id": "BAAI/bge-m3", "revision": "r"}, tmp_path, True
    )

    assert resolved == asset.resolve()


def _saved_state(torch_module: Any, path: Path, state: object) -> Path:
    torch_module.save(state, path)
    return path


def test_a_sparse_state_with_extra_keys_is_refused(torch_module: Any, tmp_path: Path) -> None:
    state = {"weight": torch_module.zeros((1, 4)), "bias": torch_module.zeros(1), "x": 1}
    asset = _saved_state(torch_module, tmp_path / "state.pt", state)

    with pytest.raises(ValueError, match="invalid state dictionary"):
        loaders._sparse_linear_state(torch_module, asset, 4)


def test_a_sparse_state_of_the_wrong_width_is_refused(torch_module: Any, tmp_path: Path) -> None:
    state = {"weight": torch_module.zeros((1, 3)), "bias": torch_module.zeros(1)}
    asset = _saved_state(torch_module, tmp_path / "state.pt", state)

    with pytest.raises(ValueError, match="invalid shape"):
        loaders._sparse_linear_state(torch_module, asset, 4)


def _hidden_and_mask(torch_module: Any) -> tuple[Any, Any]:
    hidden = torch_module.tensor(
        [[[1.0, 10.0], [3.0, 30.0], [100.0, 100.0]], [[2.0, 20.0], [4.0, 40.0], [6.0, 60.0]]]
    )
    mask = torch_module.tensor([[1, 1, 0], [1, 1, 1]])
    return hidden, mask


@pytest.mark.parametrize(
    ("pooling", "expected"),
    [
        ("cls", [[1.0, 10.0], [2.0, 20.0]]),
        ("mean", [[2.0, 20.0], [4.0, 40.0]]),
        ("last_token", [[3.0, 30.0], [6.0, 60.0]]),
    ],
)
def test_each_pooling_reads_only_the_unmasked_tokens(
    torch_module: Any, pooling: str, expected: list[list[float]]
) -> None:
    hidden, mask = _hidden_and_mask(torch_module)

    assert loaders._pooled(torch_module, hidden, mask, pooling).tolist() == expected


def test_an_unknown_pooling_is_refused(torch_module: Any) -> None:
    hidden, mask = _hidden_and_mask(torch_module)

    with pytest.raises(ValueError, match="unsupported matrix pooling: max"):
        loaders._pooled(torch_module, hidden, mask, "max")


class BatchTokenizer:
    padding_side = "left"
    truncation_side = "left"

    def __init__(self, torch_module: Any) -> None:
        self.torch = torch_module
        self.batches: list[list[str]] = []

    def __call__(self, texts: list[str], **options: object) -> dict[str, Any]:
        self.batches.append(texts)
        lengths = [float(len(text)) for text in texts]
        return {
            "input_ids": self.torch.tensor([[1, 2] for _text in texts]),
            "attention_mask": self.torch.ones((len(texts), 2), dtype=self.torch.long),
            "lengths": self.torch.tensor(lengths),
        }


class LengthModel:
    def __init__(self, torch_module: Any) -> None:
        self.torch = torch_module

    def __call__(self, **batch: Any) -> SimpleNamespace:
        lengths = batch["lengths"]
        hidden = lengths.reshape(-1, 1, 1).repeat(1, 2, 3)
        return SimpleNamespace(last_hidden_state=hidden)


def test_a_dense_only_encoder_batches_texts_and_returns_one_matrix(torch_module: Any) -> None:
    tokenizer = BatchTokenizer(torch_module)
    encoder = loaders._TransformerEncoder(torch_module, tokenizer, LengthModel(torch_module), None)

    dense = encoder(
        ["a", "bbb", "cc"],
        batch_size=2,
        max_length=16,
        pooling="cls",
        padding_side="right",
        truncation_side="right",
    )

    assert tokenizer.batches == [["a", "bbb"], ["cc"]]
    assert (tokenizer.padding_side, tokenizer.truncation_side) == ("right", "right")
    assert dense.tolist() == [[1.0] * 3, [3.0] * 3, [2.0] * 3]


@pytest.mark.parametrize(
    ("user_template", "message"),
    [
        ("{instruction} {document}", "lacks query placeholder"),
        ("{instruction} {query}", "lacks document placeholder"),
    ],
)
def test_a_qwen_template_without_a_placeholder_is_refused(user_template: str, message: str) -> None:
    formatting = {**_qwen_formatting(), "user_template": user_template}

    with pytest.raises(ValueError, match=message):
        loaders.qwen_reranker_input_ids(WordTokenizer(), formatting, "q", "d", 512)


def test_a_qwen_budget_smaller_than_its_fixed_text_is_refused() -> None:
    with pytest.raises(ValueError, match="fixed prefix and suffix exceed matrix max length"):
        loaders.qwen_reranker_input_ids(WordTokenizer(), _qwen_formatting(), "q", "d", 4)


def test_the_reranker_pads_with_eos_when_the_tokenizer_has_no_pad_token() -> None:
    tokenizer = WordTokenizer()

    assert loaders._reranker_pad_id(tokenizer) == 0
    tokenizer.pad_token_id = 7
    assert loaders._reranker_pad_id(tokenizer) == 7


def test_score_words_that_split_into_several_tokens_are_refused() -> None:
    with pytest.raises(ValueError, match="must each be one token"):
        loaders._yes_no_token_ids(WordTokenizer(), {"negative": "no", "positive": "yes indeed"})


def test_left_padding_aligns_rows_to_the_right_and_masks_the_pad(torch_module: Any) -> None:
    input_ids, attention = loaders._left_padded(torch_module, [[5, 6, 7], [8]], 0)

    assert input_ids.tolist() == [[5, 6, 7], [0, 0, 8]]
    assert attention.tolist() == [[1, 1, 1], [0, 0, 1]]


class PairTokenizer:
    def __init__(self, torch_module: Any) -> None:
        self.torch = torch_module
        self.calls: list[tuple[list[str], list[str], dict[str, object]]] = []

    def __call__(
        self, queries: list[str], documents: list[str], **options: object
    ) -> dict[str, Any]:
        self.calls.append((queries, documents, options))
        logits = [
            float(len(document) - len(query))
            for query, document in zip(queries, documents, strict=False)
        ]
        return {"pair_logits": self.torch.tensor(logits)}


class PairModel:
    def __call__(self, **encoded: Any) -> SimpleNamespace:
        return SimpleNamespace(logits=encoded["pair_logits"].reshape(-1, 1))


def test_a_pair_classifier_scores_each_pair_by_the_sigmoid_of_its_logit(torch_module: Any) -> None:
    tokenizer = PairTokenizer(torch_module)
    reranker = loaders._TransformerReranker(torch_module, tokenizer, PairModel(), {})

    scores = reranker(
        [("q", "q"), ("q", "qqq"), ("qqq", "q")],
        batch_size=2,
        max_length=32,
        contract_type="tokenizer_pair_sequence_classification",
        score_tokens={},
    )

    assert scores == pytest.approx([0.5, 1 / (1 + math.exp(-2)), 1 / (1 + math.exp(2))])
    assert [call[0] for call in tokenizer.calls] == [["q", "q"], ["qqq"]]
    assert tokenizer.calls[0][2]["truncation"] == "longest_first"
    assert tokenizer.calls[0][2]["max_length"] == 32


class YesNoModel:
    def __init__(self, torch_module: Any, tokenizer: WordTokenizer) -> None:
        self.torch = torch_module
        self.yes = tokenizer("yes")["input_ids"][0]
        self.no = tokenizer("no")["input_ids"][0]
        self.masks: list[list[list[int]]] = []

    def __call__(self, *, input_ids: Any, attention_mask: Any) -> SimpleNamespace:
        self.masks.append(attention_mask.tolist())
        vocabulary = max(self.yes, self.no) + 1
        logits = self.torch.zeros((input_ids.shape[0], input_ids.shape[1], vocabulary))
        logits[:, -1, self.yes] = attention_mask.sum(dim=1).float().log()
        return SimpleNamespace(logits=logits)


def test_a_yes_no_reranker_scores_the_softmax_of_yes_over_no(torch_module: Any) -> None:
    tokenizer = WordTokenizer()
    model = YesNoModel(torch_module, tokenizer)
    formatting = _qwen_formatting()
    reranker = loaders._TransformerReranker(torch_module, tokenizer, model, formatting)
    inputs = [{"query": "cache", "document": "the cache"}, {"query": "a", "document": "b"}]
    lengths = [
        len(
            loaders.qwen_reranker_input_ids(
                tokenizer, formatting, item["query"], item["document"], 512
            )["input_ids"]
        )
        for item in inputs
    ]

    scores = reranker(
        inputs,
        batch_size=4,
        max_length=512,
        contract_type="causal_lm_yes_no",
        score_tokens=formatting["score_tokens"],
    )

    assert scores == pytest.approx([length / (length + 1) for length in lengths])
    assert model.masks[0][1][0] == 0


class RecordingAutoClass:
    def __init__(self, label: str, calls: list[tuple[str, str, dict[str, object]]]) -> None:
        self.label = label
        self.calls = calls

    def from_pretrained(self, model_id: str, **options: object) -> SimpleNamespace:
        self.calls.append((self.label, model_id, options))
        return SimpleNamespace(eval=lambda: None)


def _install_fake_transformers(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str, dict[str, object]]]
) -> None:
    _install_module(monkeypatch, "torch", float32="float32")
    _install_module(
        monkeypatch,
        "transformers",
        AutoTokenizer=RecordingAutoClass("tokenizer", calls),
        AutoModelForCausalLM=RecordingAutoClass("causal", calls),
        AutoModelForSequenceClassification=RecordingAutoClass("classifier", calls),
    )


@pytest.mark.parametrize(
    ("reranker_id", "model_class"),
    [("BAAI/bge-reranker-v2-m3", "classifier"), ("Qwen/Qwen3-Reranker-0.6B", "causal")],
)
def test_a_reranker_loads_the_model_class_its_contract_names_from_the_offline_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reranker_id: str, model_class: str
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []
    _install_fake_transformers(monkeypatch, calls)
    selection = _selection("BAAI/bge-small-en-v1.5", "float32-384d", reranker_id)

    reranker = loaders.load_transformer_reranker(
        selection, cache_root=tmp_path, local_files_only=True, trust_remote_code=False
    )

    assert [(label, model_id) for label, model_id, _options in calls] == [
        ("tokenizer", reranker_id),
        (model_class, reranker_id),
    ]
    assert calls[1][2]["torch_dtype"] == "float32"
    assert calls[0][2] == {
        "revision": selection.reranker["revision"],
        "cache_dir": str(tmp_path),
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert reranker.formatting == selection.reranker["formatting"]


def test_a_reranker_that_asks_for_remote_code_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transformers(monkeypatch, [])
    selection = _selection("BAAI/bge-small-en-v1.5", "float32-384d", "BAAI/bge-reranker-v2-m3")

    with pytest.raises(ValueError, match="remote model code is forbidden"):
        loaders.load_transformer_reranker(
            selection, cache_root=tmp_path, local_files_only=True, trust_remote_code=True
        )


def test_a_reranker_without_its_native_library_names_the_missing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "transformers", None)
    selection = _selection("BAAI/bge-small-en-v1.5", "float32-384d", "BAAI/bge-reranker-v2-m3")

    with pytest.raises(ValueError, match="reranker requires the retrieval-benchmark extra"):
        loaders.load_transformer_reranker(
            selection, cache_root=tmp_path, local_files_only=True, trust_remote_code=False
        )


def test_an_embedder_other_than_bge_m3_loads_without_a_sparse_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []
    _install_module(monkeypatch, "torch", float32="float32")
    _install_module(
        monkeypatch,
        "transformers",
        AutoTokenizer=RecordingAutoClass("tokenizer", calls),
        AutoModel=RecordingAutoClass("model", calls),
    )

    encoder = loaders.load_transformer_embedding(
        _selection("BAAI/bge-small-en-v1.5", "float32-384d"),
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )

    assert encoder.sparse_linear is None
    assert [(label, model_id) for label, model_id, _options in calls] == [
        ("tokenizer", "BAAI/bge-small-en-v1.5"),
        ("model", "BAAI/bge-small-en-v1.5"),
    ]
