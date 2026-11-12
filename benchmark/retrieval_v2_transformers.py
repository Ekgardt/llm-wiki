"""Load pinned transformer encoders and rerankers from the offline model cache."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from retrieval_v2_contract import Candidate, ModelSelection, ScoredCandidate
from retrieval_v2_environment import sha256_file
from retrieval_v2_scoring import reciprocal_rank_fusion

BGE_M3_SPARSE_LINEAR_SHA256 = (
    "45c93804d2142b8f6d7ec6914ae23a1eee9c6a1d27d83d908a20d2afb3595ad9"
)


def _sentence_transformer_encoder(model_spec: dict, cache_root: Path, local_files_only: bool):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ValueError("model-matrix adapter requires the retrieval-benchmark extra") from exc
    model = SentenceTransformer(
        model_spec["id"],
        revision=model_spec["revision"],
        cache_folder=str(cache_root),
        local_files_only=local_files_only,
        trust_remote_code=False,
        model_kwargs={"torch_dtype": "float32"},
    )

    def encode(texts, *, batch_size, max_length, pooling, padding_side, truncation_side):
        if pooling != model_spec["inference"]["pooling"]:
            raise ValueError(f"unsupported native pooling: {pooling}")
        model.max_seq_length = max_length
        model.tokenizer.padding_side = padding_side
        model.tokenizer.truncation_side = truncation_side
        return model.encode(
            list(texts),
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )

    return encode


def _transformers_model(model_spec: dict, cache_root: Path, local_files_only: bool):
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise ValueError("model-matrix adapter requires the retrieval-benchmark extra") from exc
    common = {
        "revision": model_spec["revision"],
        "cache_dir": str(cache_root),
        "local_files_only": local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(model_spec["id"], **common)
    model = AutoModel.from_pretrained(model_spec["id"], torch_dtype=torch.float32, **common)
    model.eval()
    return torch, tokenizer, model


def _verified_sparse_asset(model_spec: dict, cache_root: Path, local_files_only: bool) -> Path:
    from huggingface_hub import hf_hub_download

    asset = Path(
        hf_hub_download(
            repo_id=model_spec["id"],
            filename="sparse_linear.pt",
            revision=model_spec["revision"],
            cache_dir=str(cache_root),
            local_files_only=local_files_only,
        )
    )
    resolved_asset = asset.resolve(strict=True)
    resolved_cache = cache_root.resolve(strict=True)
    if resolved_asset != resolved_cache and resolved_cache not in resolved_asset.parents:
        raise ValueError("BGE-M3 sparse asset escaped the model cache")
    if sha256_file(resolved_asset) != BGE_M3_SPARSE_LINEAR_SHA256:
        raise ValueError("BGE-M3 sparse_linear.pt SHA256 mismatch")
    return resolved_asset


def _sparse_linear_state(torch, resolved_asset: Path, dimensions: int) -> dict:
    state = torch.load(resolved_asset, map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or set(state) != {"weight", "bias"}:
        raise ValueError("BGE-M3 sparse_linear.pt has an invalid state dictionary")
    if tuple(state["weight"].shape) != (1, dimensions) or tuple(state["bias"].shape) != (1,):
        raise ValueError("BGE-M3 sparse_linear.pt has an invalid shape")
    return state


def _bge_m3_sparse_linear(torch, selection: ModelSelection, cache_root: Path, local_files_only: bool):
    resolved_asset = _verified_sparse_asset(selection.embedding, cache_root, local_files_only)
    dimensions = selection.variant["dimensions"]
    state = _sparse_linear_state(torch, resolved_asset, dimensions)
    sparse_linear = torch.nn.Linear(dimensions, 1, bias=True)
    sparse_linear.load_state_dict(state, strict=True)
    sparse_linear.eval()
    return sparse_linear


def _cls_pooling(torch, hidden, mask):
    del torch, mask
    return hidden[:, 0]


def _mean_pooling(torch, hidden, mask):
    del torch
    expanded = mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * expanded).sum(1) / expanded.sum(1).clamp_min(1)


def _last_token_pooling(torch, hidden, mask):
    reversed_mask = torch.flip(mask, dims=[1])
    indexes = mask.shape[1] - 1 - reversed_mask.argmax(dim=1)
    return hidden[torch.arange(hidden.shape[0]), indexes]


_POOLERS = {"cls": _cls_pooling, "mean": _mean_pooling, "last_token": _last_token_pooling}


def _pooled(torch, hidden, mask, pooling: str):
    pooler = _POOLERS.get(pooling)
    if pooler is None:
        raise ValueError(f"unsupported matrix pooling: {pooling}")
    return pooler(torch, hidden, mask)


def _unused_token_ids(tokenizer) -> set:
    return {tokenizer.cls_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id, tokenizer.unk_token_id}


def _sparse_row(token_ids: Sequence[int], weights: Sequence[float], unused: set) -> dict[str, float]:
    row: dict[str, float] = {}
    for token_id, weight in zip(token_ids, weights):
        if token_id not in unused and weight > 0:
            key = str(token_id)
            row[key] = max(row.get(key, 0.0), float(weight))
    return row


class _TransformerEncoder:
    """The encode callable of a transformers-backed embedding candidate."""

    def __init__(self, torch, tokenizer, model, sparse_linear) -> None:
        self.torch = torch
        self.tokenizer = tokenizer
        self.model = model
        self.sparse_linear = sparse_linear

    def _token_weights(self, hidden):
        with self.torch.inference_mode():
            return self.torch.relu(self.sparse_linear(hidden)).squeeze(-1).float().cpu()

    def _sparse_rows(self, batch, hidden) -> list[dict[str, float]]:
        token_weights = self._token_weights(hidden)
        unused = _unused_token_ids(self.tokenizer)
        return [
            _sparse_row(token_ids, weights, unused)
            for token_ids, weights in zip(batch["input_ids"].cpu().tolist(), token_weights.tolist())
        ]

    def _encode_batch(self, texts, max_length: int, pooling: str, sparse_rows: list):
        batch = self.tokenizer(
            list(texts), padding=True, truncation=True, max_length=max_length, return_tensors="pt"
        )
        with self.torch.inference_mode():
            hidden = self.model(**batch).last_hidden_state
        values = _pooled(self.torch, hidden, batch["attention_mask"], pooling)
        chunk = values.float().cpu().numpy()
        if self.sparse_linear is not None:
            sparse_rows.extend(self._sparse_rows(batch, hidden))
        return chunk

    def __call__(self, texts, *, batch_size, max_length, pooling, padding_side, truncation_side):
        import numpy as np

        self.tokenizer.padding_side = padding_side
        self.tokenizer.truncation_side = truncation_side
        chunks = []
        sparse_rows: list[dict[str, float]] = []
        for offset in range(0, len(texts), batch_size):
            chunks.append(self._encode_batch(texts[offset : offset + batch_size], max_length, pooling, sparse_rows))
        dense = np.concatenate(chunks, axis=0)
        if self.sparse_linear is not None:
            return {"dense_vecs": dense, "lexical_weights": sparse_rows}
        return dense


def load_transformer_embedding(
    selection: ModelSelection,
    *,
    cache_root: Path,
    local_files_only: bool,
    trust_remote_code: bool,
):
    if trust_remote_code:
        raise ValueError("remote model code is forbidden")
    model_spec = selection.embedding
    if model_spec["native_library"]["name"] == "sentence-transformers":
        return _sentence_transformer_encoder(model_spec, cache_root, local_files_only)
    torch, tokenizer, model = _transformers_model(model_spec, cache_root, local_files_only)
    sparse_linear = _optional_sparse_linear(torch, selection, cache_root, local_files_only)
    return _TransformerEncoder(torch, tokenizer, model, sparse_linear)


def _optional_sparse_linear(torch, selection: ModelSelection, cache_root: Path, local_files_only: bool):
    """BGE-M3's learned sparse head; None for every other embedder."""
    if selection.embedding["id"] != "BAAI/bge-m3":
        return None
    return _bge_m3_sparse_linear(torch, selection, cache_root, local_files_only)


def _token_ids(tokenizer, text: str) -> list[int]:
    return list(tokenizer(text, add_special_tokens=False)["input_ids"])


def _qwen_template_parts(formatting: dict) -> tuple[str, str, str]:
    query_marker = "__LLM_WIKI_QUERY__"
    document_marker = "__LLM_WIKI_DOCUMENT__"
    rendered = formatting["user_template"].format(
        instruction=formatting["instruction"], query=query_marker, document=document_marker
    )
    before_query, marker, remainder = rendered.partition(query_marker)
    if not marker:
        raise ValueError("Qwen reranker user template lacks query placeholder")
    between, marker, after_document = remainder.partition(document_marker)
    if not marker:
        raise ValueError("Qwen reranker user template lacks document placeholder")
    return before_query, between, after_document


def _trim_to_budget(query_ids: list[int], document_ids: list[int], budget: int) -> None:
    while len(query_ids) + len(document_ids) > budget:
        if len(query_ids) >= len(document_ids):
            query_ids.pop()
        else:
            document_ids.pop()


def qwen_reranker_input_ids(tokenizer, formatting: dict, query: str, document: str, max_length: int):
    before_query, between, after_document = _qwen_template_parts(formatting)
    fixed_prefix = _token_ids(tokenizer, formatting["system_prefix"] + before_query)
    fixed_middle = _token_ids(tokenizer, between)
    fixed_suffix = _token_ids(tokenizer, after_document + formatting["assistant_suffix"])
    query_ids = _token_ids(tokenizer, formatting["query_template"].format(query=query))
    document_ids = _token_ids(tokenizer, formatting["document_template"].format(document=document))
    fixed_length = len(fixed_prefix) + len(fixed_middle) + len(fixed_suffix)
    if fixed_length > max_length:
        raise ValueError("reranker fixed prefix and suffix exceed matrix max length")
    _trim_to_budget(query_ids, document_ids, max_length - fixed_length)
    return {
        "input_ids": fixed_prefix + query_ids + fixed_middle + document_ids + fixed_suffix,
        "query_tokens_kept": len(query_ids),
        "document_tokens_kept": len(document_ids),
    }


def _reranker_pad_id(tokenizer) -> int:
    if tokenizer.pad_token_id is None:
        return tokenizer.eos_token_id
    return tokenizer.pad_token_id


def _yes_no_token_ids(tokenizer, score_tokens: dict) -> tuple[int, int]:
    no_ids = tokenizer(score_tokens["negative"], add_special_tokens=False)["input_ids"]
    yes_ids = tokenizer(score_tokens["positive"], add_special_tokens=False)["input_ids"]
    if len(no_ids) != 1 or len(yes_ids) != 1:
        raise ValueError("Qwen reranker yes/no score tokens must each be one token")
    return no_ids[0], yes_ids[0]


def _left_padded(torch, rows: list[list[int]], pad_id: int):
    width = max(len(row) for row in rows)
    input_ids = torch.tensor([[pad_id] * (width - len(row)) + row for row in rows])
    attention = torch.tensor([[0] * (width - len(row)) + [1] * len(row) for row in rows])
    return input_ids, attention


class _TransformerReranker:
    """The score callable of a transformers-backed reranker candidate."""

    def __init__(self, torch, tokenizer, model, formatting: dict) -> None:
        self.torch = torch
        self.tokenizer = tokenizer
        self.model = model
        self.formatting = formatting

    def _pair_scores(self, chunk, max_length: int) -> list[float]:
        queries, documents = zip(*chunk)
        encoded = self.tokenizer(
            list(queries),
            list(documents),
            padding=True,
            truncation="longest_first",
            max_length=max_length,
            return_tensors="pt",
        )
        with self.torch.inference_mode():
            logits = self.model(**encoded).logits.float().reshape(-1)
        return self.torch.sigmoid(logits).cpu().tolist()

    def _qwen_scores(self, chunk, max_length: int, score_tokens: dict) -> list[float]:
        rows = [
            qwen_reranker_input_ids(self.tokenizer, self.formatting, item["query"], item["document"], max_length)[
                "input_ids"
            ]
            for item in chunk
        ]
        input_ids, attention = _left_padded(self.torch, rows, _reranker_pad_id(self.tokenizer))
        with self.torch.inference_mode():
            logits = self.model(input_ids=input_ids, attention_mask=attention).logits[:, -1, :].float()
        no_id, yes_id = _yes_no_token_ids(self.tokenizer, score_tokens)
        pair = self.torch.stack((logits[:, no_id], logits[:, yes_id]), dim=1)
        return self.torch.softmax(pair, dim=1)[:, 1].cpu().tolist()

    def __call__(self, inputs, *, batch_size, max_length, contract_type, score_tokens) -> list[float]:
        scores: list[float] = []
        for offset in range(0, len(inputs), batch_size):
            chunk = inputs[offset : offset + batch_size]
            if contract_type == "tokenizer_pair_sequence_classification":
                scores.extend(self._pair_scores(chunk, max_length))
                continue
            scores.extend(self._qwen_scores(chunk, max_length, score_tokens))
        return scores


def load_transformer_reranker(
    selection: ModelSelection,
    *,
    cache_root: Path,
    local_files_only: bool,
    trust_remote_code: bool,
):
    try:
        import torch
        from transformers import (
            AutoModelForCausalLM,
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
    except ImportError as exc:
        raise ValueError("reranker requires the retrieval-benchmark extra") from exc
    if trust_remote_code:
        raise ValueError("remote model code is forbidden")
    spec = selection.reranker
    formatting = spec["formatting"]
    common = {
        "revision": spec["revision"],
        "cache_dir": str(cache_root),
        "local_files_only": local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(spec["id"], **common)
    model_class = AutoModelForCausalLM
    if formatting["contract_type"] == "tokenizer_pair_sequence_classification":
        model_class = AutoModelForSequenceClassification
    model = model_class.from_pretrained(spec["id"], torch_dtype=torch.float32, **common)
    model.eval()
    return _TransformerReranker(torch, tokenizer, model, formatting)


def fuse_rankings(
    rankings: Sequence[Sequence[ScoredCandidate]], candidates: Sequence[Candidate], *, limit: int
) -> list[ScoredCandidate]:
    by_id = {candidate.evidence_id: candidate for candidate in candidates}
    fused = reciprocal_rank_fusion(
        {
            f"ranking-{index}": [(item.evidence_id, item.score) for item in ranking]
            for index, ranking in enumerate(rankings)
        },
        k=60,
    )
    return [ScoredCandidate(by_id[evidence_id], score) for evidence_id, score in fused[:limit]]
