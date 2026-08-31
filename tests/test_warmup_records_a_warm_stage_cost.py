"""Warming the encoder was necessary and was not sufficient.

`retrieval` admits an optional stage by comparing the cost the last finished
run of that kind recorded against the window the caller can offer, and only a
stage that actually ran records anything. A warm-up that merely loaded the
model therefore left the cost unknown, and the MCP budget's window is below
the ceiling an unknown-cost stage requires — so the dense leg stayed refused
until some later call happened to record a cost.

Measured on the live vault 2026-08-29, paired and interleaved, two rounds each
on a quiet machine: warming the encoder alone left call 1 answering
`['lexical']` in both rounds; warming the whole path twice put
`['lexical', 'dense']` in every call of both rounds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import retrieval  # noqa: E402
import search_memory  # noqa: E402


@pytest.fixture
def recorded_passes(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_search(query, **kwargs):
        calls.append({"query": query, **kwargs})
        return []

    monkeypatch.setattr(search_memory, "search", fake_search)
    return calls


def test_the_warm_up_runs_the_real_path_more_than_once(recorded_passes) -> None:
    """One pass pays the load and records it; only a second one runs warm."""
    mcp_server.warmup_retrieval_path(deadline_seconds=30.0)

    assert len(recorded_passes) == mcp_server.WARMUP_PASSES
    assert mcp_server.WARMUP_PASSES >= 2


def test_every_pass_asks_for_the_optional_legs(recorded_passes) -> None:
    """A pass that skips the dense leg warms nothing the dense leg needs."""
    mcp_server.warmup_retrieval_path(deadline_seconds=30.0)

    assert all(call["semantic"] for call in recorded_passes)
    assert all(call["rerank"] for call in recorded_passes)
    assert all(call["emit_telemetry"] is False for call in recorded_passes)


def test_the_pass_deadline_clears_the_unknown_cost_ceiling() -> None:
    """A window below the ceiling is refused, so the first pass records nothing."""
    window = mcp_server.WARMUP_LIMIT_SECONDS

    assert retrieval._unknown_cost_stage_fits(window) is True


def test_a_failing_pass_is_never_fatal(monkeypatch) -> None:
    """Warming is an optimisation; an unwarmed path serves as it did before."""

    def exploding_search(query, **kwargs):
        raise RuntimeError("no model here")

    monkeypatch.setattr(search_memory, "search", exploding_search)

    mcp_server.warmup_retrieval_path(deadline_seconds=30.0)


def test_the_opt_out_still_skips_the_warm_up(monkeypatch, recorded_passes) -> None:
    monkeypatch.setenv("LLMWIKI_NO_ENCODER_WARMUP", "1")

    mcp_server._start_encoder_warmup()

    assert recorded_passes == []
