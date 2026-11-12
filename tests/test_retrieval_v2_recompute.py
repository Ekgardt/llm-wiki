"""The retrieval v2 bound baseline is recomputable and verified before it is trusted."""
from __future__ import annotations

import json
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import CORPUS, ROOT

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_environment  # noqa: E402
import retrieval_v2_recompute  # noqa: E402


def test_bound_baseline_is_a_recomputable_retrieval_v2_bge_small_report(monkeypatch):
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((ROOT / matrix["selection"]["baseline"]["raw_report_path"]).read_bytes())

    assert baseline["schema_version"] == "retrieval-report/v2"
    assert baseline["effective_mode"] == "model-matrix"
    assert baseline["candidate"] == {
        "embedding": {
            "dimensions": 384,
            "id": "BAAI/bge-small-en-v1.5",
            "revision": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
            "variant_id": "float32-384d",
        },
        "reranker": None,
    }
    overall, _slices = retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)
    quality = round(
        sum(
            overall[name]
            for name in (
                "parent_recall_at_10",
                "all_required_evidence_recall_at_20",
                "ndcg_at_10",
                "mrr_at_10",
            )
        )
        / 4
        * 10_000
    )
    assert matrix["selection"]["baseline"]["overall_basis_points"] == quality
    assert matrix["selection"]["baseline"]["parent_recall_at_10_basis_points"] == round(
        overall["parent_recall_at_10"] * 10_000
    )
    assert matrix["selection"]["baseline"]["policy_sha256"] == retrieval_v2_recompute.baseline_policy_sha256(
        baseline
    )

    current_resolution = retrieval_v2_environment.locked_package_versions(ROOT / "uv.lock")
    current_resolution["numpy"] = "2.2.6"
    monkeypatch.setattr(
        retrieval_v2_environment, "locked_package_versions", lambda _lock_path: current_resolution
    )
    retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    original_version = importlib_metadata.version

    def base_environment_version(name):
        if name in {
            "jieba",
            "sentence-transformers",
            "transformers",
            "torch",
            "numpy",
            "usearch",
        }:
            raise importlib_metadata.PackageNotFoundError(name)
        return original_version(name)

    monkeypatch.setattr(importlib_metadata, "version", base_environment_version)
    retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)
    observed = retrieval_v2_environment.observed_runtime_environment("numpy-exact", lexical_config="L4")
    assert observed["packages"] == {
        "jieba": None,
        "numpy": None,
        "sentence-transformers": None,
        "torch": None,
        "transformers": None,
        "usearch": None,
    }

    evolved = json.loads(json.dumps(matrix))
    evolved["embeddings"][-1]["variants"][-1]["quality"]["status"] = "measured"
    retrieval_v2_recompute.verified_baseline_metrics(evolved, baseline, corpus_path=CORPUS)


def test_baseline_policy_fingerprint_rejects_mismatched_provenance():
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())

    matrix["selection"]["baseline"]["policy_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="policy fingerprint"):
        retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["methodology"]["environment_provenance"]["verified_lock"]["packages"][
        "numpy"
    ] = "0.0.0"
    matrix["selection"]["baseline"]["policy_sha256"] = retrieval_v2_recompute.baseline_policy_sha256(
        baseline
    )
    with pytest.raises(ValueError, match="package contract"):
        retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["benchmark_runner_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="policy fingerprint"):
        retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)


def test_baseline_verification_rejects_non_attested_quality_payload():
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["quality_claim"] = False

    with pytest.raises(ValueError, match="comparable"):
        retrieval_v2_recompute.verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)
