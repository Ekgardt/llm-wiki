"""Original pure telemetry assertions, independent from MCP."""
from __future__ import annotations

import answer_cost
import pytest
from answer_budget import estimate_tokens
from answer_cost import (
    BLOCK_TOKEN_CEILING,
    COST_KEY,
    MAX_SHARE_OF_ANSWER,
    MEASURED_BLOCK_TOKENS,
    MODE_ENV,
    attach_answer_cost,
    block_tokens,
    build_cost_block,
)


def _big_answer(tokens: int = 20_000) -> dict:
    """An envelope large enough that the block is a rounding error against it."""
    return {"components": {}, "data": {"body": "x" * (tokens * 4)}}


def _recall_envelope() -> dict:
    return {
        "components": {
            "lexical": {"generation": "g", "freshness": "fresh"},
            "dense": {"generation": "g", "freshness": "missing"},
            "graph": {"generation": "g", "freshness": "missing"},
            "reranker": {"generation": "g", "freshness": "unknown"},
        },
        "data": {
            "body": "x" * 80_000,
            "retrieval_trace": {
                "fallback_reason": "optional_stage_timeout",
                "reranker_fallback_reason": "reranker_unavailable",
            },
        },
    }


def test_the_answer_states_its_own_token_estimate_and_the_method() -> None:
    envelope = _big_answer()
    assert attach_answer_cost(envelope, elapsed_seconds=0.4, budget_seconds=10.0)
    block = envelope["data"][COST_KEY]
    assert block["tokens_estimated"] == estimate_tokens(
        {"components": {}, "data": {"body": "x" * 80_000}}
    )
    # The count is an offline estimate, not a provider-reported one, and a bare
    # integer cannot tell the caller which it is holding.
    assert block["estimate_method"] == "utf8_bytes/2"


def test_the_estimate_comes_from_the_one_shared_estimator() -> None:
    """Two token counters that disagreed would be worse than none."""
    envelope = _big_answer()
    attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10.0)
    block = envelope["data"].pop(COST_KEY)
    assert block["tokens_estimated"] == estimate_tokens(envelope)


def test_the_estimate_excludes_the_block_so_it_is_not_circular() -> None:
    envelope = _big_answer()
    attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10.0)
    with_block = estimate_tokens(envelope)
    assert envelope["data"][COST_KEY]["tokens_estimated"] < with_block


def test_wall_time_and_the_budget_it_was_measured_against_are_both_reported() -> None:
    envelope = _big_answer()
    attach_answer_cost(envelope, elapsed_seconds=1.234, budget_seconds=10.0)
    block = envelope["data"][COST_KEY]
    assert block["duration_ms"] == 1234
    assert block["budget_ms"] == 10_000


@pytest.mark.parametrize("unusable", [None, -0.5, "fast", True])
def test_an_unusable_clock_reads_as_null_never_as_zero(unusable) -> None:
    """`0` is a legal duration, so it can never double as "unknown"."""
    block = build_cost_block(_big_answer(), elapsed_seconds=unusable, budget_seconds=10.0)
    assert block["duration_ms"] is None


def test_an_unmeasurable_answer_reads_as_null_never_as_zero(monkeypatch) -> None:
    def refuse(_data):
        raise ValueError("unserialisable")

    monkeypatch.setattr(answer_cost, "estimate_tokens", refuse)
    block = build_cost_block(_big_answer(), elapsed_seconds=0.1, budget_seconds=10.0)
    assert block["tokens_estimated"] is None


def test_an_answer_of_unknown_size_is_not_charged_for_a_cost_block(monkeypatch) -> None:
    """Unknown cost cannot be shown to afford the block, so nothing is spent."""
    monkeypatch.setattr(answer_cost, "estimate_tokens", lambda _data: None)
    envelope = _big_answer()
    assert attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10.0) is False
    assert COST_KEY not in envelope["data"]


def test_a_body_that_cannot_hold_the_block_leaves_it_absent_not_empty() -> None:
    envelope = {"components": {}, "data": ["a list, not an object"]}
    assert attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10.0) is False
    assert envelope["data"] == ["a list, not an object"]


def test_the_block_says_which_optional_stages_ran_and_which_did_not() -> None:
    envelope = _recall_envelope()
    attach_answer_cost(envelope, elapsed_seconds=27.8, budget_seconds=60.0)
    stages = envelope["data"][COST_KEY]["stages"]
    assert "ran: lexical" in stages
    assert "not_run: dense, graph" in stages
    # Never folded into "ran": an unknown stage was not shown to be paid for.
    assert "unknown: reranker" in stages


def test_the_refusal_reason_is_the_planners_own_word_not_a_second_derivation() -> None:
    envelope = _recall_envelope()
    attach_answer_cost(envelope, elapsed_seconds=27.8, budget_seconds=60.0)
    trace = envelope["data"]["retrieval_trace"]
    assert envelope["data"][COST_KEY]["not_run_reason"] == trace["fallback_reason"]


def test_a_stale_stage_counts_as_run_and_an_undeclared_one_as_unknown() -> None:
    envelope = {
        "components": {
            "lexical": {"generation": "g", "freshness": "stale"},
            "graph": {"generation": "g", "freshness": "nonsense"},
        },
        "data": {"body": "x" * 80_000},
    }
    block = build_cost_block(envelope, elapsed_seconds=0.1, budget_seconds=10.0)
    assert block["stages"] == "ran: lexical; unknown: graph"
    assert "not_run_reason" not in block


def test_a_tool_with_no_optional_stages_says_nothing_about_stages() -> None:
    block = build_cost_block(_big_answer(), elapsed_seconds=0.1, budget_seconds=10.0)
    assert "stages" not in block


def test_the_block_costs_what_the_module_says_it_costs() -> None:
    plain = build_cost_block(
        {"components": {}, "data": {}}, elapsed_seconds=0.021, budget_seconds=60.0
    )
    staged = build_cost_block(_recall_envelope(), elapsed_seconds=27.83, budget_seconds=60.0)
    assert block_tokens(plain) == MEASURED_BLOCK_TOKENS["without_stages"]
    assert block_tokens(staged) == MEASURED_BLOCK_TOKENS["with_stages"]


@pytest.mark.parametrize("magnitude", [0.001, 1.0, 3600.0])
def test_the_block_never_exceeds_its_stated_ceiling(magnitude: float) -> None:
    staged = build_cost_block(
        _recall_envelope(), elapsed_seconds=magnitude, budget_seconds=magnitude
    )
    assert block_tokens(staged) <= BLOCK_TOKEN_CEILING


def test_a_cheap_answer_is_not_taxed_to_describe_itself() -> None:
    """A block worth a quarter of the answer breaks the law it reports."""
    envelope = {"components": {}, "data": {"ok": True}}
    assert attach_answer_cost(envelope, elapsed_seconds=0.02, budget_seconds=10.0) is False
    assert COST_KEY not in envelope["data"]


def test_the_block_is_attached_when_it_is_a_rounding_error() -> None:
    envelope = _big_answer()
    assert attach_answer_cost(envelope, elapsed_seconds=0.4, budget_seconds=10.0)
    block = envelope["data"][COST_KEY]
    assert block_tokens(block) <= block["tokens_estimated"] * MAX_SHARE_OF_ANSWER


def test_always_forces_the_block_onto_a_cheap_answer(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "always")
    envelope: dict[str, dict] = {"components": {}, "data": {"ok": True}}
    assert attach_answer_cost(envelope, elapsed_seconds=0.02, budget_seconds=10.0)
    assert envelope["data"][COST_KEY]["duration_ms"] == 20


def test_never_suppresses_the_block_on_an_answer_that_could_afford_it(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "never")
    envelope = _big_answer()
    assert attach_answer_cost(envelope, elapsed_seconds=0.4, budget_seconds=10.0) is False


def test_an_unreadable_mode_falls_back_to_the_measured_default(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "  ALWAYS  ")
    envelope = {"components": {}, "data": {"ok": True}}
    assert attach_answer_cost(envelope, elapsed_seconds=0.02, budget_seconds=10.0)
    monkeypatch.setenv(MODE_ENV, "yes-please")
    other = {"components": {}, "data": {"ok": True}}
    assert attach_answer_cost(other, elapsed_seconds=0.02, budget_seconds=10.0) is False


@pytest.mark.parametrize("body", ["x" * 80_000, "Привет 世界 🧭" * 8_000])
def test_repeated_attachment_measures_the_same_answer(body):
    envelope = {"components": {}, "data": {"body": body}}
    expected = estimate_tokens(envelope)
    assert attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10)
    first = envelope["data"][COST_KEY]
    assert attach_answer_cost(envelope, elapsed_seconds=0.2, budget_seconds=10)
    assert first["tokens_estimated"] == expected
    assert envelope["data"][COST_KEY]["tokens_estimated"] == expected
    assert envelope["data"][COST_KEY]["duration_ms"] == 200


def test_measurement_omits_only_the_reserved_location_without_mutation():
    telemetry = {"old": "старое" * 100}
    nested = {COST_KEY: "nested answer fact" * 100}
    data = {"body": "世界 🧭", COST_KEY: telemetry, "nested": nested}
    envelope = {"components": {}, COST_KEY: "top level fact" * 100, "data": data}
    expected = estimate_tokens({
        "components": {}, COST_KEY: envelope[COST_KEY],
        "data": {"body": data["body"], "nested": nested},
    })
    before = repr(envelope)
    block = build_cost_block(envelope, elapsed_seconds=0.1, budget_seconds=10)
    assert block["tokens_estimated"] == expected
    assert repr(envelope) == before
    assert envelope["data"] is data
    assert data[COST_KEY] is telemetry
    assert data["nested"] is nested


@pytest.mark.parametrize("bad", [{(1, 2): "invalid key"}, "\ud800"])
def test_real_estimator_failure_preserves_existing_telemetry_and_body(bad):
    telemetry = {"old": 42}
    data = {COST_KEY: telemetry, "body": bad}
    envelope = {"components": {}, "data": data}
    before = repr(envelope)
    block = build_cost_block(envelope, elapsed_seconds=0.1, budget_seconds=10)
    assert block["tokens_estimated"] is None
    assert repr(envelope) == before
    assert envelope["data"] is data
    assert data[COST_KEY] is telemetry
    assert data["body"] is bad


def test_real_circular_body_failure_does_not_damage_the_input():
    body = []
    body.append(body)
    telemetry = {"old": 42}
    data = {"body": body, COST_KEY: telemetry}
    envelope = {"components": {}, "data": data}
    block = build_cost_block(envelope, elapsed_seconds=0.1, budget_seconds=10)
    assert block["tokens_estimated"] is None
    assert envelope["data"] is data
    assert data[COST_KEY] is telemetry
    assert data["body"] is body
    assert body[0] is body


def test_invalid_old_telemetry_is_excluded_before_real_estimation():
    telemetry = {(1, 2): "invalid JSON key"}
    envelope = {"components": {}, "data": {COST_KEY: telemetry, "body": "é"}}
    block = build_cost_block(envelope, elapsed_seconds=0.1, budget_seconds=10)
    assert block["tokens_estimated"] == estimate_tokens({"components": {}, "data": {"body": "é"}})
    assert envelope["data"][COST_KEY] is telemetry


@pytest.mark.parametrize("mode", ["auto", "never"])
def test_refused_attachment_keeps_the_old_block(monkeypatch, mode):
    monkeypatch.setenv(MODE_ENV, mode)
    telemetry = {"old": 42}
    data = {"body": "small", COST_KEY: telemetry}
    envelope = {"components": {}, "data": data}
    assert not attach_answer_cost(envelope, elapsed_seconds=0.1, budget_seconds=10)
    assert envelope["data"] is data
    assert data[COST_KEY] is telemetry
