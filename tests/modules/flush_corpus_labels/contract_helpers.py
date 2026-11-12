"""Helpers shared by test_contract.py and the test files importing them."""
import sys
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

import flush_corpus_labels  # noqa: E402


def _builder():
    return flush_corpus_labels
