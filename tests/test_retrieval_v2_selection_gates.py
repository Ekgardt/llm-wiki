"""The retrieval v2 lexical winner needs complete comparable evidence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_selection_gates  # noqa: E402


def test_lexical_winner_requires_complete_comparable_l0_through_l4_evidence():
    common = {
        "schema_version": "retrieval-report/v2",
        "corpus_sha256": "c" * 64,
        "benchmark_contract_sha256": "b" * 64,
        "benchmark_runner_sha256": "r" * 64,
        "acquisition_mode": None,
        "adapter_kind": "deterministic-fake",
        "effective_mode": "deterministic-fake",
    }
    reports = []
    for index, level in enumerate(("L0", "L1", "L2", "L3", "L4")):
        reports.append(
            {
                **common,
                "methodology": {
                    "lexical_configuration": json.loads(
                        json.dumps(retrieval_v2_contract.LEXICAL_CONFIGURATIONS[level])
                    )
                },
                "overall": {
                    "parent_recall_at_10": 0.8 + index / 100,
                    "all_required_evidence_recall_at_20": 0.8,
                    "ndcg_at_10": 0.8,
                    "mrr_at_10": 0.8,
                    "no_answer_false_answer_rate": 0.0,
                },
                "measurements": {"warm_latency_p95_ms": 10.0 + index},
            }
        )

    assert retrieval_v2_selection_gates.select_lexical_winner(reports)["id"] == "L4"
    with pytest.raises(ValueError, match="complete.*L0.*L4"):
        retrieval_v2_selection_gates.select_lexical_winner(reports[:-1])
    reports[-1]["corpus_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="comparable"):
        retrieval_v2_selection_gates.select_lexical_winner(reports)
    reports[-1]["corpus_sha256"] = "c" * 64
    reports[-1]["methodology"]["lexical_configuration"] = {"id": "L4", "extra": True}
    with pytest.raises(ValueError, match="configuration"):
        retrieval_v2_selection_gates.select_lexical_winner(reports)
