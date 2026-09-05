"""Two things the stand could not price, because it did not record them.

Measured 2026-09-01: 28 of 200 answers were produced by the model and then
destroyed by `verify_grounded_answer`, and the result row kept only the gate's
message. So the accuracy of what the strictest rule discards could only be
bounded — 0 to +0.14 overall — never priced. The reply is recorded now; it
changes no answer and no verdict.

And the answer window was a constant, 28 672 bytes, chosen to keep the prompt
near the 7 000-token envelope Mem0's cost claim describes. The reader accepts
two hundred thousand. Whether that ceiling costs accuracy is an arm of the
stand now, not a number in the source.

See `docs/research/2026-09-02-where-a-multiple-could-come-from.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_vault  # noqa: E402


def _generate(metrics: dict, reply: str | None, monkeypatch) -> str | None:
    import llm_client

    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: reply)
    generator = longmemeval_vault._instrumented_generator(metrics)
    return generator("prompt", "system", 512)


def test_the_reply_is_recorded_so_a_discarded_answer_can_be_judged(monkeypatch):
    metrics: dict = {}

    _generate(metrics, "the user paid 85 dollars", monkeypatch)

    assert metrics["raw_reply"] == "the user paid 85 dollars"


def test_an_empty_reply_is_recorded_as_empty_not_missing(monkeypatch):
    metrics: dict = {}

    _generate(metrics, None, monkeypatch)

    assert metrics["raw_reply"] == ""


def test_a_huge_reply_is_bounded(monkeypatch):
    """A result row is evidence, not a transcript store."""
    metrics: dict = {}

    _generate(metrics, "x" * 50_000, monkeypatch)

    assert len(metrics["raw_reply"]) == 8000


def test_the_stock_window_is_unchanged_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("LLMWIKI_BENCH_ANSWER_BUDGET", raising=False)

    assert longmemeval_vault._answer_budget() == longmemeval_vault.ANSWER_INPUT_BUDGET


def test_the_window_can_be_set_per_arm(monkeypatch):
    monkeypatch.setenv("LLMWIKI_BENCH_ANSWER_BUDGET", "120000")

    assert longmemeval_vault._answer_budget() == 120_000


def test_a_nonsense_window_falls_back_to_the_stock_one(monkeypatch):
    """A typo in an env var must not silently run an arm nobody asked for."""
    monkeypatch.setenv("LLMWIKI_BENCH_ANSWER_BUDGET", "wide")

    assert longmemeval_vault._answer_budget() == longmemeval_vault.ANSWER_INPUT_BUDGET


def test_the_window_never_drops_below_a_single_span(monkeypatch):
    """Below a chunk the answer refuses itself and the arm measures nothing."""
    monkeypatch.setenv("LLMWIKI_BENCH_ANSWER_BUDGET", "10")

    assert longmemeval_vault._answer_budget() == 4096


def test_the_stock_candidate_count_is_unchanged_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("LLMWIKI_BENCH_QA_CANDIDATES", raising=False)

    assert longmemeval_vault._qa_candidates() == longmemeval_vault.QA_CANDIDATES


def test_retrieval_depth_can_be_set_per_arm(monkeypatch):
    """Widening the window changed nothing; all twelve candidates already fit."""
    monkeypatch.setenv("LLMWIKI_BENCH_QA_CANDIDATES", "40")

    assert longmemeval_vault._qa_candidates() == 40


def test_a_nonsense_depth_falls_back_to_the_stock_one(monkeypatch):
    monkeypatch.setenv("LLMWIKI_BENCH_QA_CANDIDATES", "deep")

    assert longmemeval_vault._qa_candidates() == longmemeval_vault.QA_CANDIDATES


def test_the_depth_is_bounded_at_both_ends(monkeypatch):
    monkeypatch.setenv("LLMWIKI_BENCH_QA_CANDIDATES", "0")
    assert longmemeval_vault._qa_candidates() == 1

    monkeypatch.setenv("LLMWIKI_BENCH_QA_CANDIDATES", "100000")
    assert longmemeval_vault._qa_candidates() == 200
