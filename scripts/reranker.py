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
from typing import Any

from provenance import authority_weight

# Lazy-loaded model + tokenizer cache (kept together).
_reranker_bundle: dict[str, Any] | None = None

IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_RERANK_DEPTH = 20
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
        import onnxruntime  # noqa: F401
        import optimum.onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401

        return True
    except ImportError:
        return False


def _get_reranker_bundle() -> dict[str, Any] | None:
    """Lazy-load model and tokenizer together. Returns None if unavailable."""
    global _reranker_bundle
    if _reranker_bundle is not None:
        return _reranker_bundle
    identity = configured_reranker_identity()
    if identity is None:
        return None
    if not _have_reranker_deps():
        return None
    try:
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from transformers import AutoTokenizer

        model_name, revision = identity
        model = ORTModelForSequenceClassification.from_pretrained(
            model_name,
            file_name="onnx/model.onnx",
            revision=revision,
            local_files_only=True,
            trust_remote_code=False,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            revision=revision,
            local_files_only=True,
            trust_remote_code=False,
        )
        _reranker_bundle = {
            "model": model,
            "tokenizer": tokenizer,
            "model_id": model_name,
            "model_revision": revision,
        }
        return _reranker_bundle
    except Exception:
        return None


def _authority_weight_of(item: dict) -> float:
    """Reuse the weight fusion recorded; fall back to the item's own authority."""
    recorded = item.get("authority_weight")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        return float(recorded)
    return authority_weight(item.get("authority"))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def should_rerank(
    *,
    profile: str,
    candidates: list[dict[str, Any]],
    analysis_intents: tuple[str, ...] | list[str] = (),
    rerank_enabled: bool = True,
) -> tuple[bool, str | None]:
    """Decide whether reranking should run. Returns (apply, skip_reason)."""
    if not rerank_enabled:
        return False, "rerank_disabled"
    profile_u = (profile or "BASE").upper()
    if profile_u not in RERANK_PROFILES:
        return False, "profile_bypass"
    if len(candidates) <= 1:
        return False, "tiny_result_set"
    intents = set(analysis_intents or ())
    if intents & {"quoted_phrase", "exact_identifier"}:
        return False, "exact_match_bypass"
    if profile_u in {"EXACT", "DIRECT"}:
        return False, "exact_match_bypass"

    # Explicit synthesis / cross-language ambiguity only (not every question).
    if profile_u == "GLOBAL" or "global_synthesis" in intents:
        return True, None
    if "cross_language" in intents:
        return True, None

    # Rank disagreement: top lexical vs top dense differ.
    top = candidates[0]
    bm25_rank = top.get("bm25_rank")
    vector_rank = top.get("vector_rank")
    if isinstance(bm25_rank, int) and isinstance(vector_rank, int):
        if bm25_rank == 1 and vector_rank != 1:
            return True, None
        if vector_rank == 1 and bm25_rank != 1:
            return True, None

    # Close top scores.
    if len(candidates) >= 2:
        s0 = float(candidates[0].get("rrf_score") or candidates[0].get("score") or 0.0)
        s1 = float(candidates[1].get("rrf_score") or candidates[1].get("score") or 0.0)
        if s0 > 0 and abs(s0 - s1) / s0 <= 0.05:
            return True, None

    return False, "conditions_unmet"


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
        return documents[:limit] if limit > 0 else list(documents)

    started = time.perf_counter()
    depth = max(1, int(depth))
    head = documents[:depth]
    tail = documents[depth:]

    scores: list[float] | None = None
    used_model_id = model_id
    used_revision = model_revision
    fallback_reason: str | None = None

    if scorer is not None:
        pairs = []
        for doc in head:
            doc_text = doc.get(text_field, "") or doc.get("title", "") or ""
            pairs.append((query, str(doc_text)))
        try:
            scores = [float(v) for v in scorer(pairs)]
            used_model_id = used_model_id or "fake-cross-encoder"
            used_revision = used_revision or "test"
        except Exception:
            fallback_reason = "reranker_error"
            scores = None
    else:
        bundle = _get_reranker_bundle()
        if bundle is None:
            for doc in documents:
                doc.setdefault("reranker_applied", False)
                doc.setdefault("reranker_fallback_reason", "reranker_unavailable")
            return documents[:limit] if limit > 0 else list(documents)
        try:
            import torch

            tokenizer = bundle["tokenizer"]
            model = bundle["model"]
            used_model_id = bundle["model_id"]
            used_revision = bundle["model_revision"]
            pairs = []
            for doc in head:
                doc_text = doc.get(text_field, "") or doc.get("title", "") or ""
                pairs.append((query, str(doc_text)))
            if not pairs:
                return documents[:limit] if limit > 0 else list(documents)
            inputs = tokenizer(
                pairs, padding=True, truncation=True, max_length=512, return_tensors="pt"
            )
            with torch.no_grad():
                logits = model(**inputs).logits.squeeze(-1).tolist()
            if isinstance(logits, float):
                scores = [float(logits)]
            else:
                scores = [float(v) for v in logits]
        except Exception:
            fallback_reason = "reranker_error"
            scores = None

    duration_ms = int((time.perf_counter() - started) * 1000)

    if scores is None:
        for doc in documents:
            doc["reranker_applied"] = False
            doc["reranker_fallback_reason"] = fallback_reason or "reranker_unavailable"
            doc["reranker_model_id"] = used_model_id
            doc["reranker_model_revision"] = used_revision
            doc["reranker_depth"] = depth
            doc["reranker_duration_ms"] = duration_ms
        return documents[:limit] if limit > 0 else list(documents)

    head_scored: list[dict] = []
    for index, doc in enumerate(head):
        item = dict(doc)
        raw = scores[index] if index < len(scores) else 0.0
        normalized = _sigmoid(raw)
        rrf = float(item.get("rrf_score") or item.get("score") or 0.0)
        # Typed provenance weighs on the score that decides the order here too,
        # or a reranked list would silently drop the trust contract that fusion
        # applied. The weight is the one fusion computed for this candidate.
        weight = _authority_weight_of(item)
        final = (RERANK_BLEND_RERANK * normalized + RERANK_BLEND_RRF * rrf) * weight
        item["rerank_score"] = round(raw, 6)
        item["rerank_score_normalized"] = round(normalized, 6)
        item["final_score"] = round(final, 6)
        item["score"] = item["final_score"]
        item["reranker_applied"] = True
        item["reranker_fallback_reason"] = None
        item["reranker_model_id"] = used_model_id
        item["reranker_model_revision"] = used_revision
        item["reranker_depth"] = depth
        item["reranker_duration_ms"] = duration_ms
        head_scored.append(item)

    head_scored.sort(
        key=lambda d: (
            -float(d.get("final_score") or 0.0),
            str(d.get("candidate_id") or d.get("path") or d.get("slug") or ""),
        )
    )
    # Preserve non-reranked tail in original order after the reranked prefix.
    tail_kept = []
    for doc in tail:
        item = dict(doc)
        item.setdefault("rrf_score", item.get("score"))
        fused = float(item.get("rrf_score") or item.get("score") or 0.0)
        item.setdefault("final_score", round(fused * _authority_weight_of(item), 6))
        item["reranker_applied"] = True
        item["reranker_prefix"] = False
        item["reranker_model_id"] = used_model_id
        item["reranker_model_revision"] = used_revision
        item["reranker_depth"] = depth
        item["reranker_duration_ms"] = duration_ms
        item["reranker_fallback_reason"] = None
        tail_kept.append(item)

    merged = head_scored + tail_kept
    return merged[:limit] if limit > 0 else merged


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
