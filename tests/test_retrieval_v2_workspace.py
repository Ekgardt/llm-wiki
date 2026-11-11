"""The retrieval v2 prefetch emits a non-quality receipt without running queries."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.retrieval_v2_helpers import CORPUS

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_matrix  # noqa: E402
import retrieval_v2_transformers  # noqa: E402
import retrieval_v2_workspace  # noqa: E402


def test_prefetch_emits_non_quality_receipt_without_running_queries(tmp_path, monkeypatch):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    calls = []
    monkeypatch.setattr(
        retrieval_v2_transformers,
        "load_transformer_embedding",
        lambda *args, **kwargs: calls.append("embedding"),
    )
    receipt = retrieval_v2_workspace.prefetch_models(selection, cache_root=tmp_path / "cache")

    assert calls == ["embedding"]
    assert receipt["artifact_kind"] == "model-acquisition-receipt"
    assert receipt["quality_claim"] is False
    assert receipt["acquisition_mode"] == "download-allowed-explicit-cache"
