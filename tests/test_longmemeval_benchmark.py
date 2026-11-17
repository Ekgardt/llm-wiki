"""Offline tests for the LongMemEval harness (MEM-10).

Everything here runs without the dataset, without the network, and without a
provider: what is tested is the deterministic machinery — sampling, scoring,
transcript shaping — not the benchmark result itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_score  # noqa: E402
import longmemeval_vault  # noqa: E402


def test_haystack_dates_become_day_and_time() -> None:
    assert longmemeval_vault.day_of("2023/05/20 (Sat) 02:21") == "2023-05-20"
    assert longmemeval_vault.time_of("2023/05/20 (Sat) 02:21") == "02:21:00"


def test_the_transcript_renders_through_the_products_own_renderer() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_evidence import render_transcript

    turns = [
        {"role": "user", "content": "I adopted a puppy named Biscuit."},
        {"role": "assistant", "content": "Congratulations!", "has_answer": True},
    ]
    rendered = render_transcript(longmemeval_vault.transcript_jsonl(turns))

    assert "**user:** I adopted a puppy named Biscuit." in rendered
    assert "**assistant:** Congratulations!" in rendered


def test_the_daily_block_is_capture_shaped_and_dated() -> None:
    block = longmemeval_vault.daily_block("sess-1", "2023/05/20 (Sat) 02:21", "**user:** hi")

    assert block.startswith("## [02:21:00] session_end | sess-1")
    assert "_captured: 2023-05-20 02:21:00_" in block


def test_the_hypothesis_joins_claim_texts() -> None:
    document = {
        "claims": [{"text": "You paid 85 dollars."}, {"text": "The sneakers were new."}],
    }

    assert longmemeval_vault.hypothesis_of(document) == (
        "You paid 85 dollars. The sneakers were new."
    )


def test_provider_failures_get_their_own_error_kind() -> None:
    provider = longmemeval_vault.error_kind(
        ValueError("grounded QA provider returned no response")
    )

    assert provider == "provider_no_response"
    assert provider in longmemeval_score.PROVIDER_ERROR_KINDS


def test_a_deadline_is_a_provider_failure_and_a_gate_is_not() -> None:
    deadline = longmemeval_vault.error_kind(TimeoutError("grounded QA deadline exceeded"))
    gate = longmemeval_vault.error_kind(ValueError("citation precision and recall gates"))

    assert deadline in longmemeval_score.PROVIDER_ERROR_KINDS
    assert gate == "verification_or_gate"
