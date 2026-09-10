"""Cross-encoder reranker for precision search.

Re-ranks fused candidates using a cross-encoder that scores each
(query, document) pair jointly. Runs on CPU via ONNX Runtime when available.

Install: uv sync --locked --no-default-groups --inexact --extra reranker
The default is the approved `BAAI/bge-reranker-v2-m3` at its pinned revision;
the environment may name another model ID with an immutable 40-hex revision,
or `LLMWIKI_RERANKER_MODEL=off` to run without one.
"""
from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from provenance import authority_weight, type_weight

# Lazy-loaded model + tokenizer cache (kept together), and the reason the one
# load attempt failed when it did — a load that failed once fails the same way
# on every question, so it is not retried per question (the encoder in
# `search_memory._get_embedder` records its reason the same way).
_reranker_bundle: dict[str, Any] | None = None
_reranker_unavailable_reason: str | None = None

IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")
# The product default (owner's decision 2026-09-10): the matrix-approved
# multilingual cross-encoder at its pinned revision. Measured on the
# cross-lingual corpus, it takes a Russian question over English pages from
# MRR 0.60 to 0.98 on top of the shipped encoder, where swapping the encoder
# gained at most 0.04. See
# `docs/research/2026-09-10-cross-lingual-memory-world-practice.md`.
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
# The weights file at that revision, verified by `scripts/install_models.py`.
DEFAULT_RERANKER_WEIGHTS_SHA256 = (
    "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"
)
DEFAULT_RERANKER_WEIGHTS_BYTES = 2271071852
RERANKER_OFF = "off"
# Ten passages of up to 512 tokens are about 3.5 s at int8 on four loaded
# cores; twenty were 4.2 s and overran the optional stage's share on the MCP
# path. Depths 10, 20 and 50 ranked the cross-lingual corpus identically.
DEFAULT_RERANK_DEPTH = 10
# The approved matrix entry for BAAI/bge-reranker-v2-m3 declares 512 tokens.
RERANK_MAX_TOKENS = 512
# Pairs scored together. Small enough that a short passage does not pay for
# a long one, large enough to keep the four cores of the slowest supported
# machine busy. See `_cross_encoder_scores`.
RERANK_BATCH_PAIRS = 2
RERANK_BLEND_RERANK = 0.6
RERANK_BLEND_RRF = 0.4

# Profiles that may invoke the reranker when conditions match.
RERANK_PROFILES = frozenset({"HYBRID", "GLOBAL", "GRAPH", "TEMPORAL", "BASE"})


def configured_reranker_identity() -> tuple[str, str] | None:
    """The reranker to load: the environment's, or the product default.

    An environment that names nothing gets the default. `off` gets none. A
    partial or mutable identity is refused, not repaired: a model without a
    pinned revision is not something this product will load.
    """
    model = os.environ.get("LLMWIKI_RERANKER_MODEL", "").strip()
    revision = os.environ.get("LLMWIKI_RERANKER_REVISION", "").strip()
    if not model and not revision:
        return DEFAULT_RERANKER_MODEL, DEFAULT_RERANKER_REVISION
    if model.lower() == RERANKER_OFF:
        return None
    return _explicit_identity(model, revision)


def _explicit_identity(model: str, revision: str) -> tuple[str, str] | None:
    """A named model with a pinned revision, or nothing."""
    if not model:
        return None
    if not IMMUTABLE_REVISION.fullmatch(revision):
        return None
    return model, revision


def _have_reranker_deps() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401

        return True
    except ImportError:
        return False


def _loaded_bundle(model_name: str, revision: str) -> dict[str, Any]:
    """The pinned cross-encoder, loaded from local files only.

    Loaded through `transformers`, the library the approved model matrix names
    for this architecture and the one the benchmark already uses. The previous
    loader asked optimum for `onnx/model.onnx`, and the approved revision of
    `BAAI/bge-reranker-v2-m3` ships no ONNX at all — so the runtime reranker
    could never load the only reranker the product approves.
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    common = {
        "revision": revision,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(model_name, **common)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name, dtype=torch.float32, **common
    )
    model.eval()
    model, precision = _cpu_precision(model)
    return {
        "model": model,
        "tokenizer": tokenizer,
        "model_id": model_name,
        "model_revision": revision,
        "precision": precision,
    }


# The reranker's Linear layers at int8, decided at load time. Measured
# 2026-09-07 on four cores: twelve pairs at 512 tokens took 10.0 s in fp32
# with the machine quiet and 18–21 s beside other work, against a 12 s stage
# bound — so on 483 of 600 benchmark questions the stage was abandoned and
# the reranker scored nothing. Int8 takes 4.2 s and keeps Spearman 0.81–0.95
# with the fp32 order. Same weights, same revision, no new files.
# See `docs/research/2026-09-07-a-reranker-that-never-finished.md`.
PRECISION_ENV = "LLMWIKI_RERANKER_PRECISION"
FP32 = "fp32"
INT8_DYNAMIC = "int8-dynamic"


def requested_precision() -> str:
    """What the operator asked for: int8 unless told fp32 by name."""
    if os.environ.get(PRECISION_ENV, "").strip().lower() == FP32:
        return FP32
    return INT8_DYNAMIC


def _cpu_precision(model: Any) -> tuple[Any, str]:
    """The model at the requested precision, and the name of what it got."""
    import torch

    # A stand-in `torch` in tests has no `nn`; nothing that is not a real
    # module is quantised, and nothing about it is assumed.
    module_type = getattr(getattr(torch, "nn", None), "Module", ())
    if requested_precision() == FP32 or not isinstance(model, module_type):
        return model, FP32
    # In place, or the model is deep-copied and the copy owns every weight —
    # measured 2026-09-08: the copy took the process to 3.6 GB of anonymous
    # memory and the kernel killed a stand worker at 3.8 GB, while in place
    # it is 1.07 GB anonymous against 0.8 GB for fp32, the rest file-backed
    # and evictable. The freed heap is handed back to the OS afterwards.
    # Deprecated in torch 2.13 in favour of torchao, which is not installed;
    # the call still works and the replacement is one line when it is.
    quantized = torch.ao.quantization.quantize_dynamic(
        model, {torch.nn.Linear}, dtype=torch.qint8, inplace=True
    )
    _release_heap()
    return quantized, INT8_DYNAMIC


def _release_heap() -> None:
    """Return what quantisation freed to the OS; glibc keeps it otherwise."""
    import ctypes
    import gc
    import sys

    gc.collect()
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        return


def _get_reranker_bundle() -> dict[str, Any] | None:
    """Lazy-load model and tokenizer together. Returns None if unavailable."""
    global _reranker_bundle, _reranker_unavailable_reason
    if _reranker_bundle is not None:
        return _reranker_bundle
    if _reranker_unavailable_reason is not None:
        return None
    identity = _loadable_identity()
    if identity is None:
        return None
    try:
        _reranker_bundle = _loaded_bundle(*identity)
    except Exception as exc:  # noqa: BLE001 - an unloadable reranker degrades one stage
        _reranker_unavailable_reason = f"{type(exc).__name__}: {exc}"[:512]
        return None
    return _reranker_bundle


def _loadable_identity() -> tuple[str, str] | None:
    """The configured identity when the libraries that load it are present."""
    identity = configured_reranker_identity()
    if identity is None or not _have_reranker_deps():
        return None
    return identity


def reranker_unavailable_reason() -> str | None:
    """Why the configured reranker could not be loaded, once it was tried."""
    return _reranker_unavailable_reason


def _recorded_weight(item: dict, key: str, fallback: float) -> float:
    """The weight fusion recorded, or the fallback when it did not record one."""
    recorded = item.get(key)
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        return float(recorded)
    return fallback


def _trust_weight_of(item: dict) -> float:
    """Both factors of the trust weight, as fusion computed them for this item."""
    authority = _recorded_weight(
        item, "authority_weight", authority_weight(item.get("authority"))
    )
    page = _recorded_weight(item, "type_weight", type_weight(item.get("type")))
    return authority * page


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _fused_score(item: Mapping[str, Any]) -> float:
    return float(item.get("rrf_score") or item.get("score") or 0.0)


def _unavailable_reason(
    rerank_enabled: bool, profile: str, candidates: Sequence[Any]
) -> str | None:
    checks = (
        (not rerank_enabled, "rerank_disabled"),
        (profile not in RERANK_PROFILES, "profile_bypass"),
        (len(candidates) <= 1, "tiny_result_set"),
    )
    return next((reason for failed, reason in checks if failed), None)


def _exact_bypass_reason(profile: str, intents: set[str]) -> str | None:
    if intents & {"quoted_phrase", "exact_identifier"}:
        return "exact_match_bypass"
    if profile in {"EXACT", "DIRECT"}:
        return "exact_match_bypass"
    return None


def _rerank_refusal(
    profile: str, candidates: Sequence[Any], intents: set[str], rerank_enabled: bool
) -> str | None:
    """A reason not to rerank at all, or None."""
    unavailable = _unavailable_reason(rerank_enabled, profile, candidates)
    if unavailable is not None:
        return unavailable
    return _exact_bypass_reason(profile, intents)


def should_rerank(
    *,
    profile: str,
    candidates: list[dict[str, Any]],
    analysis_intents: tuple[str, ...] | list[str] = (),
    rerank_enabled: bool = True,
) -> tuple[bool, str | None]:
    """Rerank every question unless there is a named reason not to.

    Until 2026-09-10 the stage also needed a trigger — a synthesis profile,
    a question mixing two scripts, the lexical and dense top hits disagreeing,
    or the top two fused scores within 5 %. A Russian question over English
    pages matched none of them, and that is the question the reranker is for:
    measured on the cross-lingual corpus it takes that case from MRR 0.60 to
    0.98, and the monolingual questions from 0.73 to 0.99 besides.
    """
    profile_u = (profile or "BASE").upper()
    intents = set(analysis_intents or ())
    refusal = _rerank_refusal(profile_u, candidates, intents, rerank_enabled)
    if refusal is not None:
        return False, refusal
    return True, None

def _limited(documents: list[dict], limit: int) -> list[dict]:
    if limit > 0:
        return documents[:limit]
    return list(documents)


def _query_pairs(head: Sequence[Mapping[str, Any]], query: str, text_field: str) -> list:
    pairs = []
    for doc in head:
        doc_text = doc.get(text_field, "") or doc.get("title", "") or ""
        pairs.append((query, str(doc_text)))
    return pairs


class _Scoring(NamedTuple):
    """What one scoring attempt produced; `scores` is None when it failed."""

    scores: list[float] | None
    model_id: str | None
    model_revision: str | None
    fallback_reason: str | None


def _score_with_scorer(
    scorer: Any,
    head: Sequence[Mapping[str, Any]],
    query: str,
    text_field: str,
    model_id: str | None,
    model_revision: str | None,
) -> _Scoring:
    """A test's deterministic fake cross-encoder."""
    pairs = _query_pairs(head, query, text_field)
    try:
        scores = [float(value) for value in scorer(pairs)]
    except Exception:  # noqa: BLE001 - a failed scorer keeps the fused order
        return _Scoring(None, model_id, model_revision, "reranker_error")
    return _Scoring(
        scores,
        model_id or "fake-cross-encoder",
        model_revision or "test",
        None,
    )


class _OutOfTime(Exception):
    """The rerank budget ran out; the fused order stands unchanged."""


def _require_time(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _OutOfTime("rerank budget exhausted")


def _batch_logits(bundle: Mapping[str, Any], pairs: list) -> list[float]:
    """Raw logits for one batch, padded to the longest passage it contains."""
    import torch

    queries = [pair[0] for pair in pairs]
    documents = [pair[1] for pair in pairs]
    inputs = bundle["tokenizer"](
        queries,
        documents,
        padding=True,
        truncation="longest_first",
        max_length=RERANK_MAX_TOKENS,
        return_tensors="pt",
    )
    with torch.inference_mode():
        logits = bundle["model"](**inputs).logits.float().reshape(-1).tolist()
    if isinstance(logits, float):
        return [float(logits)]
    return [float(value) for value in logits]


def _batches_by_length(pairs: list) -> list[list[int]]:
    """Positions grouped so each batch pads only to its own longest passage."""
    order = sorted(range(len(pairs)), key=lambda index: len(pairs[index][1]))
    return [
        order[start : start + RERANK_BATCH_PAIRS]
        for start in range(0, len(order), RERANK_BATCH_PAIRS)
    ]


def _cross_encoder_scores(
    bundle: Mapping[str, Any], pairs: list, *, deadline: float | None = None
) -> list[float]:
    """Raw logits for (query, passage) pairs; the caller squashes them.

    One batch of twenty pairs pads every pair to the longest of them, and the
    longest is nearly always the 512-token ceiling. Measured on this vault on
    2026-09-06, the twenty candidates of a real query tokenize to 61…512
    tokens: half the arithmetic of a single batch was padding. Sorting by
    length first and scoring in small batches pays for the padding inside each
    batch only, and the scores are the same — padding is masked, so the two
    orders agree to 1e-5, which is float noise and not a rank.
    """
    scores = [0.0] * len(pairs)
    for batch in _batches_by_length(pairs):
        _require_time(deadline)
        chosen = [pairs[index] for index in batch]
        for index, score in zip(batch, _batch_logits(bundle, chosen)):
            scores[index] = score
    return scores


def _score_with_bundle(
    bundle: Mapping[str, Any],
    head: Sequence[Mapping[str, Any]],
    query: str,
    text_field: str,
    deadline: float | None = None,
) -> _Scoring:
    """Scores for the head, or a named reason the fused order should stand.

    Half-reranked is not an order: a score from the cross-encoder and a fused
    score do not live on the same scale, so a budget that runs out mid-way
    abandons the whole stage rather than mixing them. Losing the reranker
    costs some precision. Losing the answer, which is what a raised deadline
    did to `mcp.recall` 67 times on this vault, costs all of it.
    """
    model_id = bundle["model_id"]
    revision = bundle["model_revision"]
    pairs = _query_pairs(head, query, text_field)
    if not pairs:
        return _Scoring([], model_id, revision, None)
    try:
        scores = _cross_encoder_scores(bundle, pairs, deadline=deadline)
    except _OutOfTime:
        return _Scoring(None, model_id, revision, "reranker_deadline")
    except Exception:  # noqa: BLE001 - a failed reranker keeps the fused order
        return _Scoring(None, model_id, revision, "reranker_error")
    return _Scoring(scores, model_id, revision, None)


def _mark_not_applied(documents: list[dict], reason: str) -> None:
    for doc in documents:
        doc.setdefault("reranker_applied", False)
        doc.setdefault("reranker_fallback_reason", reason)


def _mark_failed(documents: list[dict], scoring: _Scoring, depth: int, duration_ms: int) -> None:
    for doc in documents:
        doc["reranker_applied"] = False
        doc["reranker_fallback_reason"] = scoring.fallback_reason or "reranker_unavailable"
        doc["reranker_model_id"] = scoring.model_id
        doc["reranker_model_revision"] = scoring.model_revision
        doc["reranker_depth"] = depth
        doc["reranker_duration_ms"] = duration_ms


def _stamp_run(item: dict, scoring: _Scoring, depth: int, duration_ms: int) -> None:
    item["reranker_applied"] = True
    item["reranker_fallback_reason"] = None
    item["reranker_model_id"] = scoring.model_id
    item["reranker_model_revision"] = scoring.model_revision
    item["reranker_depth"] = depth
    item["reranker_duration_ms"] = duration_ms


def _scored_item(
    doc: Mapping[str, Any], raw: float, scoring: _Scoring, depth: int, duration_ms: int
) -> dict:
    item = dict(doc)
    normalized = _sigmoid(raw)
    # Typed provenance weighs on the score that decides the order here too, or a
    # reranked list would silently drop the trust contract that fusion applied.
    # The weight is the one fusion computed for this candidate.
    final = (
        RERANK_BLEND_RERANK * normalized + RERANK_BLEND_RRF * _fused_score(item)
    ) * _trust_weight_of(item)
    item["rerank_score"] = round(raw, 6)
    item["rerank_score_normalized"] = round(normalized, 6)
    item["final_score"] = round(final, 6)
    item["score"] = item["final_score"]
    _stamp_run(item, scoring, depth, duration_ms)
    return item


def _scored_head(
    head: Sequence[Mapping[str, Any]],
    scoring: _Scoring,
    depth: int,
    duration_ms: int,
) -> list[dict]:
    scores = scoring.scores or []
    head_scored = [
        _scored_item(
            doc,
            scores[index] if index < len(scores) else 0.0,
            scoring,
            depth,
            duration_ms,
        )
        for index, doc in enumerate(head)
    ]
    head_scored.sort(key=_final_score_order)
    return head_scored


def _final_score_order(item: Mapping[str, Any]) -> tuple[float, str]:
    identity = item.get("candidate_id") or item.get("path") or item.get("slug") or ""
    return -float(item.get("final_score") or 0.0), str(identity)


def _kept_tail(
    tail: Sequence[Mapping[str, Any]], scoring: _Scoring, depth: int, duration_ms: int
) -> list[dict]:
    """Documents beyond the reranked prefix keep their fused order."""
    tail_kept = []
    for doc in tail:
        item = dict(doc)
        item.setdefault("rrf_score", item.get("score"))
        item.setdefault("final_score", round(_fused_score(item) * _trust_weight_of(item), 6))
        _stamp_run(item, scoring, depth, duration_ms)
        item["reranker_prefix"] = False
        tail_kept.append(item)
    return tail_kept


def _scoring_for(
    head: Sequence[Mapping[str, Any]],
    query: str,
    text_field: str,
    scorer: Any | None,
    model_id: str | None,
    model_revision: str | None,
    deadline: float | None = None,
) -> _Scoring | None:
    """None means there is no reranker at all, which is not a failure."""
    if scorer is not None:
        return _score_with_scorer(
            scorer, head, query, text_field, model_id, model_revision
        )
    bundle = _get_reranker_bundle()
    if bundle is None:
        return None
    return _score_with_bundle(bundle, head, query, text_field, deadline)


def rerank(
    query: str,
    documents: list[dict],
    limit: int = 10,
    text_field: str = "summary",
    *,
    depth: int = DEFAULT_RERANK_DEPTH,
    scorer: Any | None = None,
    model_id: str | None = None,
    model_revision: str | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Re-rank documents; preserve tail beyond depth; blend into final_score.

    ``scorer`` is an optional callable(list[tuple[str,str]]) -> list[float]
    used by tests as a deterministic fake cross-encoder.

    ``deadline`` is a `time.monotonic` instant. Past it the stage is abandoned
    and every document keeps its fused order, marked `reranker_deadline`.
    """
    if not documents or not query.strip():
        return _limited(documents, limit)
    started = time.perf_counter()
    depth = max(1, int(depth))
    scoring = _scoring_for(
        documents[:depth], query, text_field, scorer, model_id, model_revision, deadline
    )
    if scoring is None:
        _mark_not_applied(documents, "reranker_unavailable")
        return _limited(documents, limit)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return _limited(_reranked(documents, scoring, depth, duration_ms), limit)


def _reranked(
    documents: list[dict], scoring: _Scoring, depth: int, duration_ms: int
) -> list[dict]:
    """The head in the reranker's order and the tail behind it, or the fused order marked."""
    if scoring.scores is None:
        _mark_failed(documents, scoring, depth, duration_ms)
        return documents
    head, tail = documents[:depth], documents[depth:]
    return _scored_head(head, scoring, depth, duration_ms) + _kept_tail(
        tail, scoring, depth, duration_ms
    )

def reranker_available() -> bool:
    """Quick probe: is the reranker model loaded and ready?"""
    return _get_reranker_bundle() is not None


if __name__ == "__main__":
    identity = configured_reranker_identity()
    if identity is None:
        print("Reranker not configured.")
    elif not _have_reranker_deps():
        print("Reranker dependencies not installed.")
    elif reranker_available():
        print("Reranker loaded locally.")
    else:
        print("Reranker configured but not present locally.")
