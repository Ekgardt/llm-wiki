"""The retrieval v2 scoring tokenizes and fuses rankings deterministically."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_scoring  # noqa: E402


def test_actual_fts_tokenization_matches_unicode_and_english_porter_contracts():
    assert set(retrieval_v2_scoring.fts5_tokens("Caf\u00e9 \u041a\u0430\u0442\u0430\u043b\u043e\u0433", "unicode61 remove_diacritics 2")) == {
        "cafe",
        "\u043a\u0430\u0442\u0430\u043b\u043e\u0433",
    }
    assert retrieval_v2_scoring.fts5_tokens(
        "running corrected", "porter unicode61 remove_diacritics 2"
    ) == ("correct", "run")


def test_fixed_rank_fusion_is_deterministic_and_ignores_bm25_magnitudes():
    rankings = {
        "unicode": [("b", -0.0001), ("a", -1000.0)],
        "english_porter": [("a", -0.1), ("b", -0.2)],
    }

    first = retrieval_v2_scoring.reciprocal_rank_fusion(rankings, k=60)
    second = retrieval_v2_scoring.reciprocal_rank_fusion(dict(reversed(list(rankings.items()))), k=60)

    assert first == second
    assert [evidence_id for evidence_id, _score in first] == ["a", "b"]
    assert first[0][1] == first[1][1]
    assert first[0][1] == pytest.approx((1 / 61 + 1 / 62) / (2 / 61))
    assert all(0.0 <= score <= 1.0 for _evidence_id, score in first)
    assert retrieval_v2_scoring.reciprocal_rank_fusion(
        {"one": [("winner", -1.0)], "two": [("winner", -999.0)]}, k=60
    ) == [("winner", 1.0)]
    assert retrieval_v2_scoring.reciprocal_rank_fusion(
        {"one": [("winner", -1.0), ("winner", -2.0)]}, k=60
    ) == [("winner", 1.0)]
