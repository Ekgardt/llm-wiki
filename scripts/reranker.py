"""Cross-encoder reranker for precision search.

Re-ranks fused candidates using a cross-encoder that scores each
(query, document) pair jointly. Runs on CPU via ONNX Runtime when available.

Install: uv sync --locked --no-default-groups --inexact --extra reranker
Configure one model ID and immutable 40-hex revision in the environment.
"""
from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from provenance import authority_weight, type_weight

# Lazy-loaded model + tokenizer cache (kept together).
_reranker_bundle: dict[str, Any] | None = None

IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_RERANK_DEPTH = 20
# The approved matrix entry for BAAI/bge-reranker-v2-m3 declares 512 tokens.
RERANK_MAX_TOKENS = 512
RERANK_BLEND_RERANK = 0.6
RERANK_BLEND_RRF = 0.4

# Profiles that may invoke the reranker when conditions match.
RERANK_PROFILES = frozenset({"HYBRID", "GLOBAL", "GRAPH", "TEMPORAL", "BASE"})


def configured_reranker_identity() -> tuple[str, str] | None:
    model = os.environ.get("LLMWIKI_RERANKER_MODEL", "").strip()
    revision = os.environ.get("LLMWIKI_RERANKER_REVISION", "").strip()
    if not model and not revision:
        return None
    if not model or not IMMUTABLE_REVISION.fullmatch(revision):
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
        model_name, torch_dtype=torch.float32, **common
    )
    model.eval()
    return {
        "model": model,
        "tokenizer": tokenizer,
        "model_id": model_name,
        "model_revision": revision,
    }


def _get_reranker_bundle() -> dict[str, Any] | None:
    """Lazy-load model and tokenizer together. Returns None if unavailable."""
    global _reranker_bundle
    if _reranker_bundle is not None:
        return _reranker_bundle
    identity = configured_reranker_identity()
    if identity is None or not _have_reranker_deps():
        return None
    try:
        _reranker_bundle = _loaded_bundle(*identity)
    except Exception:  # noqa: BLE001 - an unloadable reranker degrades one stage
        return None
    return _reranker_bundle


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
    if not rerank_enabled:
        return "rerank_disabled"
    if profile not in RERANK_PROFILES:
        return "profile_bypass"
    if len(candidates) <= 1:
        return "tiny_result_set"
    return None


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


def _explicitly_wanted(profile: str, intents: set[str]) -> bool:
    """Explicit synthesis or cross-language ambiguity only, not every question."""
    if profile == "GLOBAL" or "global_synthesis" in intents:
        return True
    return "cross_language" in intents


def _ranks_disagree(top: Mapping[str, Any]) -> bool:
    """The top lexical hit and the top dense hit are different documents."""
    bm25_rank = top.get("bm25_rank")
    vector_rank = top.get("vector_rank")
    if not isinstance(bm25_rank, int) or not isinstance(vector_rank, int):
        return False
    return (bm25_rank == 1) != (vector_rank == 1)


def _scores_are_close(candidates: Sequence[Mapping[str, Any]]) -> bool:
    if len(candidates) < 2:
        return False
    first = _fused_score(candidates[0])
    second = _fused_score(candidates[1])
    return first > 0 and abs(first - second) / first <= 0.05


def _rerank_wanted(
    profile: str, candidates: Sequence[Mapping[str, Any]], intents: set[str]
) -> bool:
    if _explicitly_wanted(profile, intents):
        return True
    if _ranks_disagree(candidates[0]):
        return True
    return _scores_are_close(candidates)


def should_rerank(
    *,
    profile: str,
    candidates: list[dict[str, Any]],
    analysis_intents: tuple[str, ...] | list[str] = (),
    rerank_enabled: bool = True,
) -> tuple[bool, str | None]:
    """Decide whether reranking should run. Returns (apply, skip_reason)."""
    profile_u = (profile or "BASE").upper()
    intents = set(analysis_intents or ())
    refusal = _rerank_refusal(profile_u, candidates, intents, rerank_enabled)
    if refusal is not None:
        return False, refusal
    if _rerank_wanted(profile_u, candidates, intents):
        return True, None
    return False, "conditions_unmet"

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


def _cross_encoder_scores(bundle: Mapping[str, Any], pairs: list) -> list[float]:
    """Raw logits for (query, passage) pairs; the caller squashes them."""
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


def _score_with_bundle(
    bundle: Mapping[str, Any],
    head: Sequence[Mapping[str, Any]],
    query: str,
    text_field: str,
) -> _Scoring:
    model_id = bundle["model_id"]
    revision = bundle["model_revision"]
    pairs = _query_pairs(head, query, text_field)
    if not pairs:
        return _Scoring([], model_id, revision, None)
    try:
        return _Scoring(_cross_encoder_scores(bundle, pairs), model_id, revision, None)
    except Exception:  # noqa: BLE001 - a failed reranker keeps the fused order
        return _Scoring(None, model_id, revision, "reranker_error")


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
) -> _Scoring | None:
    """None means there is no reranker at all, which is not a failure."""
    if scorer is not None:
        return _score_with_scorer(
            scorer, head, query, text_field, model_id, model_revision
        )
    bundle = _get_reranker_bundle()
    if bundle is None:
        return None
    return _score_with_bundle(bundle, head, query, text_field)


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
) -> list[dict]:
    """Re-rank documents; preserve tail beyond depth; blend into final_score.

    ``scorer`` is an optional callable(list[tuple[str,str]]) -> list[float]
    used by tests as a deterministic fake cross-encoder.
    """
    if not documents or not query.strip():
        return _limited(documents, limit)
    started = time.perf_counter()
    depth = max(1, int(depth))
    head, tail = documents[:depth], documents[depth:]
    scoring = _scoring_for(head, query, text_field, scorer, model_id, model_revision)
    if scoring is None:
        _mark_not_applied(documents, "reranker_unavailable")
        return _limited(documents, limit)
    duration_ms = int((time.perf_counter() - started) * 1000)
    if scoring.scores is None:
        _mark_failed(documents, scoring, depth, duration_ms)
        return _limited(documents, limit)
    merged = _scored_head(head, scoring, depth, duration_ms) + _kept_tail(
        tail, scoring, depth, duration_ms
    )
    return _limited(merged, limit)

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
