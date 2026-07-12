"""Cross-encoder reranker for precision search (mxbai-rerank-base-v1).

Re-ranks the top-N search results using a cross-encoder model that scores
each (query, document) pair jointly — much more accurate than bi-encoder
similarity. Runs on CPU via ONNX Runtime, no API calls.

The reranker is applied AFTER BM25+Vector+Graph fusion (triple-fusion RRF).
It takes the top-20 fused candidates and reorders them by cross-encoder
relevance score, keeping only the top-limit.

Install: uv sync --extra reranker
Model:   BAAI/bge-reranker-base (MIT, ~75MB ONNX INT8)
         OR mixedbread-ai/mxbai-rerank-base-v1 (Apache-2.0, ~75MB ONNX INT8)
"""
from __future__ import annotations

import os

# Lazy-loaded model cache.
_reranker: object | None = None
_reranker_model_name: str | None = None

# Default model — MIT license, proven by Memtrace, multilingual.
# Override via LLMWIKI_RERANKER_MODEL env var.
DEFAULT_RERANKER = os.environ.get(
    "LLMWIKI_RERANKER_MODEL", "BAAI/bge-reranker-base"
)


def _have_reranker_deps() -> bool:
    """Check if onnxruntime + optimum + tokenizers are importable."""
    try:
        import onnxruntime  # noqa: F401
        import optimum.onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_reranker():
    """Lazy-load the cross-encoder model via ONNX Runtime. Returns None if unavailable."""
    global _reranker, _reranker_model_name
    if _reranker is not None:
        return _reranker
    if not _have_reranker_deps():
        return None
    try:
        from optimum.onnxruntime import ORTModelForSequenceClassification

        model_name = DEFAULT_RERANKER
        _reranker = ORTModelForSequenceClassification.from_pretrained(
            model_name, file_name="onnx/model.onnx"
        )
        _reranker_model_name = model_name
        return _reranker
    except Exception:
        return None


def rerank(
    query: str,
    documents: list[dict],
    limit: int = 10,
    text_field: str = "summary",
) -> list[dict]:
    """Re-rank documents by cross-encoder relevance score.

    Args:
        query: The search query.
        documents: List of result dicts (from BM25+Vector+Graph fusion).
        limit: Maximum results to return.
        text_field: Which field to use as document text for scoring.
                    Options: "summary", "title", "body".

    Returns:
        Reordered list of result dicts with updated "score" fields.
        Falls back to input order if reranker unavailable.
    """
    if not documents or not query.strip():
        return documents[:limit]

    reranker = _get_reranker()
    if reranker is None:
        return documents[:limit]

    try:
        import torch
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(DEFAULT_RERANKER)

        # Build (query, doc) pairs for cross-encoder.
        pairs = []
        for doc in documents[:20]:  # Only rerank top-20 for speed
            doc_text = doc.get(text_field, "") or doc.get("title", "")
            if not doc_text:
                doc_text = doc.get("title", "")
            pairs.append((query, doc_text))

        if not pairs:
            return documents[:limit]

        # Score each pair.
        inputs = tokenizer(
            pairs, padding=True, truncation=True, max_length=512, return_tensors="pt"
        )
        with torch.no_grad():
            scores = reranker(**inputs).logits.squeeze(-1).tolist()

        # Attach reranker scores and re-sort.
        for i, doc in enumerate(documents[:20]):
            doc["rerank_score"] = float(scores[i]) if i < len(scores) else 0.0

        # Sort by reranker score (higher = more relevant).
        reranked = sorted(documents[:20], key=lambda x: x.get("rerank_score", 0), reverse=True)

        # Update score field to blended RRF + reranker.
        for doc in reranked:
            original = doc.get("score", 0)
            rerank_s = doc.get("rerank_score", 0)
            # Blend: 60% reranker + 40% original RRF (keeps some signal diversity)
            doc["score"] = round(0.6 * _sigmoid(rerank_s) + 0.4 * original, 4)

        return reranked[:limit]
    except Exception:
        return documents[:limit]


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid for score normalization."""
    if x >= 0:
        import math
        return 1.0 / (1.0 + math.exp(-x))
    else:
        import math
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)


def reranker_available() -> bool:
    """Quick probe: is the reranker model loaded and ready?"""
    return _get_reranker() is not None


if __name__ == "__main__":
    if _have_reranker_deps():
        print(f"Reranker dependencies available. Model: {DEFAULT_RERANKER}")
        if reranker_available():
            print("Reranker loaded successfully.")
        else:
            print("Reranker model not yet downloaded. First use will download it.")
    else:
        print("Reranker dependencies not installed. Run: uv sync --extra reranker")
