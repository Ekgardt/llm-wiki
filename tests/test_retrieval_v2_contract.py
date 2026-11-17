"""The retrieval v2 benchmark ships its artifacts and a closed lexical configuration contract."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.retrieval_v2_helpers import CORPUS, RUNNER, SCHEMA

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402


def test_required_v2_artifacts_exist():
    assert SCHEMA.is_file()
    assert CORPUS.is_file()
    assert RUNNER.is_file()


def test_lexical_configurations_are_explicit_and_closed():
    assert set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS) == {"L0", "L1", "L2", "L3", "L4"}
    assert retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L0"] == {
        "id": "L0",
        "indexes": {"unicode": "unicode61 remove_diacritics 2"},
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": None,
    }
    assert set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L1"]["indexes"]) == {
        "unicode",
        "english_porter",
    }
    assert retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L1"]["indexes"]["english_porter"] == (
        "porter unicode61 remove_diacritics 2"
    )
    assert set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L2"]["indexes"]) == {
        "unicode",
        "chinese_trigram",
    }
    assert set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L3"]["indexes"]) == {
        "unicode",
        "chinese_jieba",
    }
    assert set(retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L4"]["indexes"]) == {
        "unicode",
        "english_porter",
        "chinese_trigram",
        "chinese_jieba",
    }
    assert retrieval_v2_contract.LEXICAL_CONFIGURATIONS["L3"]["segmentation"] == {
        "dependency": "jieba",
        "version": "0.42.1",
        "HMM": False,
    }
    assert all(
        retrieval_v2_contract.LEXICAL_CONFIGURATIONS[level]["fusion"]
        == {"method": "reciprocal-rank-fusion", "k": 60}
        for level in ("L1", "L2", "L3", "L4")
    )
    assert all(
        set(configuration)
        == {"id", "indexes", "routing", "fallback", "segmentation", "fusion"}
        for configuration in retrieval_v2_contract.LEXICAL_CONFIGURATIONS.values()
    )
