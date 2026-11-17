"""Original LongMemEval cost-per-correct-answer assertions remain qualified separately."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))



def test_the_report_prices_a_right_answer_in_tokens() -> None:
    """Accuracy alone chose the widest window; economy alone would choose the
    weakest. Neither is the quantity that matters, and until 2026-09-06 nobody
    computed the one that is.

    On the 2026-09-03 sweep this reads 86.8, 52.7, 20.4 and 15.3 for answer
    budgets of 12 288, 32 768, 122 880 and 262 144 — and it reverses the reading
    taken from accuracy alone.
    """
    import longmemeval_score

    rows = [
        {"judge_correct": True, "est_total_prompt_tokens": 1_000_000},
        {"judge_correct": False, "est_total_prompt_tokens": 1_000_000},
    ]

    assert longmemeval_score._correct_per_million_tokens(rows) == 0.5


def test_a_ratio_without_a_judge_is_no_number() -> None:
    import longmemeval_score

    rows = [{"est_total_prompt_tokens": 1_000_000}]

    assert longmemeval_score._correct_per_million_tokens(rows) is None


def test_a_run_that_spent_nothing_prices_nothing() -> None:
    import longmemeval_score

    rows = [{"judge_correct": True, "est_total_prompt_tokens": 0}]

    assert longmemeval_score._correct_per_million_tokens(rows) is None
