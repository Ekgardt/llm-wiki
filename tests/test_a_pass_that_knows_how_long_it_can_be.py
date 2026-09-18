"""A scheduled pass counts everything it can do, and stops at its own bound.

`worst_case_seconds()` said "every step's timeout and every wait" while leaving
the checkout update, the tail tasks and the operator's wait override out of the
sum, and the step margins covered one provider where one call walks the whole
order (audit M-B2, M-B3, I-A14). launchd and cron limit a pass not at all, so
the pass now limits itself. Research:
`docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_client  # noqa: E402
import scheduled_nightly  # noqa: E402
import self_update  # noqa: E402


def test_one_call_is_every_candidate_in_turn(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORY_LLM_TIMEOUT_S", raising=False)
    auto = llm_client.worst_case_call_seconds()

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")
    forced = llm_client.worst_case_call_seconds()

    assert (auto, forced) == (llm_client.DEFAULT_TIMEOUT_S * 5, llm_client.DEFAULT_TIMEOUT_S)


def test_an_operator_timeout_widens_the_margin_it_would_have_broken(monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT_S", "600")

    margin = scheduled_nightly.provider_margin_seconds()

    assert margin == scheduled_nightly.STEP_START_MARGIN_SECONDS + 600


def test_a_step_that_stops_at_its_budget_leaves_room_for_the_last_call(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORY_LLM_TIMEOUT_S", raising=False)
    from fact_keys import DEFAULT_BUDGET_SECONDS

    episodes = scheduled_nightly._episode_step()
    keys = scheduled_nightly._fact_keys_step()
    room = scheduled_nightly.provider_margin_seconds()

    assert episodes.timeout - scheduled_nightly.EPISODE_BUDGET_SECONDS == room
    assert keys.timeout - int(DEFAULT_BUDGET_SECONDS) == room


def test_the_sum_counts_the_checkout_update_and_the_tail(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_COMPILE_WAIT_SECONDS", raising=False)
    update = self_update.WORST_CASE_SECONDS
    counted = scheduled_nightly.worst_case_seconds()

    monkeypatch.setattr(self_update, "WORST_CASE_SECONDS", 0.0)

    assert counted - scheduled_nightly.worst_case_seconds() == update
    assert update == 2 * 120.0 + 600.0 + 13 * 60.0


def test_the_sum_follows_the_wait_the_operator_set(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_COMPILE_WAIT_SECONDS", raising=False)
    default = scheduled_nightly.worst_case_seconds()

    monkeypatch.setenv("MEMORY_COMPILE_WAIT_SECONDS", "3600")
    widened = scheduled_nightly.worst_case_seconds()

    assert widened - default == 3600 - scheduled_nightly.COMPILE_WAIT_SECONDS


def test_a_pass_past_its_own_bound_stops_at_the_next_step() -> None:
    run_step = scheduled_nightly._fenced_step_runner(None, deadline=0.0)

    with pytest.raises(scheduled_nightly.PassBoundExceeded):
        run_step(["true"], print, "step", timeout=1)


def test_a_pass_inside_its_bound_runs_its_step(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        scheduled_nightly, "_run_step", lambda command, log, name, *, timeout: seen.append(name) or 0
    )
    run_step = scheduled_nightly._fenced_step_runner(None, deadline=float("inf"))

    assert run_step(["true"], print, "step", timeout=1) == 0
    assert seen == ["step"]
