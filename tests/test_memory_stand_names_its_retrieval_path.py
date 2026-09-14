"""A memory number must say which legs of retrieval were live when it was taken.

The 500-question run of 2026-09-13 was launched from a worktree without torch or
transformers: `vector_state` came back `absent` on 498 questions, the reranker was
unavailable on 442, and the 0.770 it printed read like the product's number while
measuring lexical search alone. Research:
`docs/research/2026-09-13-why-we-lose-the-memory-questions.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import run_longmemeval  # noqa: E402


def _row(vector_state: str = "complete", reranker_reason: str | None = None) -> dict:
    return {"vector_state": vector_state, "reranker_fallback_reason": reranker_reason}


def test_the_full_path_reports_no_reason_to_doubt_the_number():
    path = run_longmemeval.retrieval_path([_row(), _row()])

    assert path == {"rows": 2, "vectors_complete": 2, "reranker_unavailable": 0}
    assert run_longmemeval.degraded_reasons(path) == []


def test_a_run_without_vectors_is_named_degraded():
    path = run_longmemeval.retrieval_path([_row(vector_state="absent"), _row()])

    reasons = run_longmemeval.degraded_reasons(path)

    assert reasons == ["no vectors on 1 of 2 questions"]


def test_an_unavailable_reranker_is_named_degraded():
    path = run_longmemeval.retrieval_path(
        [_row(reranker_reason="reranker_unavailable"), _row()]
    )

    reasons = run_longmemeval.degraded_reasons(path)

    assert reasons == ["reranker unavailable on 1 of 2"]


def test_a_legitimate_reranker_bypass_is_not_degradation():
    """EXACT and TEMPORAL profiles bypass the reranker by design."""
    path = run_longmemeval.retrieval_path(
        [_row(reranker_reason="profile_bypass"), _row(reranker_reason="exact_match_bypass")]
    )

    assert run_longmemeval.degraded_reasons(path) == []


def test_both_legs_missing_are_both_named():
    path = run_longmemeval.retrieval_path(
        [_row(vector_state="absent", reranker_reason="reranker_unavailable")]
    )

    assert len(run_longmemeval.degraded_reasons(path)) == 2


def test_a_degraded_run_does_not_exit_zero():
    assert run_longmemeval._exit_code(["no vectors on 1 of 2 questions"]) == (
        run_longmemeval.DEGRADED_EXIT
    )


def test_a_full_run_exits_zero():
    assert run_longmemeval._exit_code([]) == 0


def test_an_empty_run_claims_nothing():
    path = run_longmemeval.retrieval_path([])

    assert run_longmemeval.degraded_reasons(path) == []
