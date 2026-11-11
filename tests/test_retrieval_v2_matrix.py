"""The retrieval v2 model matrix selection is exact, pinned and fail-closed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes

from tests.retrieval_v2_helpers import CORPUS, spec_identity

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_environment  # noqa: E402
import retrieval_v2_matrix  # noqa: E402


def _embedding_variants(matrix) -> set[tuple[str, str]]:
    return {
        (candidate["id"], variant["variant_id"])
        for candidate in matrix["embeddings"]
        for variant in candidate["variants"]
    }


def _expected_matrix(embedding_variants, rerankers) -> set:
    return {
        (model_id, variant_id, reranker_id)
        for model_id, variant_id in embedding_variants
        for reranker_id in {None, *rerankers}
    }


def test_model_matrix_selection_is_exact_pinned_and_fail_closed(tmp_path):
    matrix_path = BENCHMARK / "model-matrix-v1.json"
    selection = retrieval_v2_matrix.load_model_selection(
        matrix_path,
        CORPUS,
        model_id="Qwen/Qwen3-Embedding-0.6B",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
    )

    assert selection.embedding["revision"] == (
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    )
    assert selection.variant["dimensions"] == 384
    assert selection.reranker["revision"] == (
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    )
    assert selection.matrix_sha256 == retrieval_v2_environment.sha256_file(matrix_path)
    assert selection.corpus_sha256 == retrieval_v2_environment.sha256_file(CORPUS)

    with pytest.raises(ValueError, match="unknown embedding"):
        retrieval_v2_matrix.load_model_selection(
            matrix_path, CORPUS, model_id="unknown/model", variant_id="float32-384d"
        )

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    gemma = next(
        item for item in matrix["embeddings"] if item["id"] == "google/embeddinggemma-300m"
    )
    gemma["shipping_eligible"] = True
    changed = tmp_path / "matrix.json"
    changed.write_bytes(canonical_json_bytes(matrix) + b"\n")
    with pytest.raises(ValueError, match="shipping policy"):
        retrieval_v2_matrix.load_model_selection(
            changed,
            CORPUS,
            model_id=matrix["embeddings"][0]["id"],
            variant_id=matrix["embeddings"][0]["variants"][0]["variant_id"],
        )


def test_required_candidate_specs_cover_the_closed_canonical_matrix():
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())

    specs = retrieval_v2_matrix.required_candidate_specs(matrix)
    embedding_variants = _embedding_variants(matrix)
    rerankers = {candidate["id"] for candidate in matrix["rerankers"]}
    observed = {spec_identity(spec) for spec in specs}

    assert len(embedding_variants) == 9
    assert rerankers == {"BAAI/bge-reranker-v2-m3", "Qwen/Qwen3-Reranker-0.6B"}
    assert observed == _expected_matrix(embedding_variants, rerankers)
    assert len(specs) == len(observed) == 27
    assert any(
        spec["embedding"]["id"] == "google/embeddinggemma-300m" for spec in specs
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda matrix: matrix["selection"].update(unknown=True),
        lambda matrix: matrix["selection"]["baseline"].update(unknown=True),
        lambda matrix: matrix["selection"]["limits"].update(unknown=True),
        lambda matrix: matrix["selection"]["aggregation_evidence_contract"].update(
            unknown=True
        ),
        lambda matrix: matrix["lexical"].update(unknown=True),
        lambda matrix: matrix["embeddings"][0]["variants"][0]["quality"].update(
            unknown=True
        ),
    ],
)
def test_model_matrix_runtime_rejects_unknown_nested_policy_fields(tmp_path, mutate):
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_text(encoding="utf-8"))
    mutate(matrix)
    changed = tmp_path / "matrix.json"
    changed.write_bytes(canonical_json_bytes(matrix) + b"\n")

    with pytest.raises(ValueError, match="closed canonical matrix object"):
        retrieval_v2_matrix.load_model_selection(
            changed,
            CORPUS,
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
        )
