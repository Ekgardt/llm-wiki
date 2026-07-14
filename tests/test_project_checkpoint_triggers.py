from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from project_journal import CheckpointReducer, ProjectProjection, build_handoff

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def reducer() -> CheckpointReducer:
    return CheckpointReducer(host_progress_signals=True)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "pre_compact"}, "before_compaction"),
        ({"type": "compaction_confirmed"}, "after_compaction"),
        ({"type": "decision"}, "decision"),
        ({"type": "correction"}, "correction"),
        ({"type": "blocker_opened"}, "blocker_change"),
        ({"type": "blocker_closed"}, "blocker_change"),
        ({"type": "task_completed"}, "task_completed"),
        ({"type": "task_cancelled"}, "task_cancelled"),
        ({"type": "ownership_transferred"}, "ownership_transfer"),
        ({"type": "significant_failure"}, "significant_failure"),
        ({"type": "file_changed", "significant": True}, "file_change"),
        ({"type": "public_contract_changed"}, "public_contract_change"),
        ({"type": "test_result_changed"}, "test_result_change"),
        ({"type": "session_end", "dirty": True}, "session_end"),
    ],
)
def test_exact_checkpoint_triggers(event, expected):
    decision = CheckpointReducer(host_progress_signals=True).observe(event, now=NOW)
    assert decision is not None
    assert decision.reason == expected


def test_token_thresholds_fire_at_60_then_every_10_and_force_80(reducer):
    assert reducer.observe({"type": "token_usage", "percent": 59}, now=NOW) is None
    assert reducer.observe({"type": "token_usage", "percent": 60}, now=NOW).reason == "token_60"
    assert reducer.observe({"type": "token_usage", "percent": 69}, now=NOW) is None
    assert reducer.observe({"type": "token_usage", "percent": 70}, now=NOW).reason == "token_70"
    decision = reducer.observe({"type": "token_usage", "percent": 80}, now=NOW)
    assert decision.reason == "token_forced_80"
    assert decision.forced is True
    assert reducer.observe({"type": "token_usage", "percent": 81}, now=NOW) is None


def test_dirty_elapsed_thresholds_are_checked_only_on_observed_events():
    reducer = CheckpointReducer(host_progress_signals=True)
    assert reducer.observe({"type": "mutation", "dirty": True}, now=NOW) is None
    assert reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=9)) is None
    assert (
        reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=11)).reason
        == "dirty_10_minutes"
    )
    assert reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=29)) is None
    assert (
        reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=31)).reason
        == "dirty_30_minutes"
    )


def test_debounced_dirty_threshold_remains_pending_for_next_event():
    reducer = CheckpointReducer(host_progress_signals=True)
    assert reducer.observe({"type": "mutation", "dirty": True}, now=NOW) is None
    assert reducer.observe({"type": "correction"}, now=NOW + timedelta(minutes=9, seconds=50))
    assert reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=10)) is None
    assert (
        reducer.observe({"type": "read"}, now=NOW + timedelta(minutes=10, seconds=21)).reason
        == "dirty_10_minutes"
    )


def test_first_observation_above_60_does_not_skip_token_threshold():
    reducer = CheckpointReducer(host_progress_signals=True)
    assert reducer.observe({"type": "token_usage", "percent": 75}, now=NOW).reason == "token_60"


@pytest.mark.parametrize("event_type", ["stop", "session_idle", "session_end"])
def test_dirty_stop_idle_and_end_checkpoint(event_type):
    decision = CheckpointReducer().observe(
        {"type": event_type, "dirty": True}, now=NOW
    )
    assert decision is not None
    assert decision.reason == {
        "stop": "dirty_stop",
        "session_idle": "dirty_idle",
        "session_end": "session_end",
    }[event_type]


def test_session_start_always_requests_recovery():
    decision = CheckpointReducer().observe({"type": "session_start"}, now=NOW)
    assert decision.reason == "session_start_recovery"


def test_ordinary_triggers_are_debounced_for_30_seconds():
    reducer = CheckpointReducer(host_progress_signals=True)
    assert reducer.observe({"type": "correction"}, now=NOW).reason == "correction"
    assert reducer.observe({"type": "blocker_opened"}, now=NOW + timedelta(seconds=29)) is None
    assert (
        reducer.observe({"type": "blocker_opened"}, now=NOW + timedelta(seconds=30)).reason
        == "blocker_change"
    )


@pytest.mark.parametrize(
    "event",
    [
        {"type": "pre_compact"},
        {"type": "compaction_confirmed"},
        {"type": "decision"},
        {"type": "task_completed"},
        {"type": "task_cancelled"},
        {"type": "significant_failure"},
        {"type": "ownership_transferred"},
        {"type": "session_end", "dirty": True},
    ],
)
def test_bypass_triggers_ignore_ordinary_debounce(event):
    reducer = CheckpointReducer(host_progress_signals=True)
    assert reducer.observe({"type": "correction"}, now=NOW)
    assert reducer.observe(event, now=NOW + timedelta(seconds=1)) is not None


def test_every_twentieth_significant_event_is_fallback_without_host_signals():
    reducer = CheckpointReducer(host_progress_signals=False)
    for index in range(1, 20):
        assert (
            reducer.observe(
                {"type": "mutation", "changed": True, "event_id": f"event-{index}"},
                now=NOW + timedelta(seconds=index),
            )
            is None
        )
    assert (
        reducer.observe(
            {"type": "mutation", "changed": True, "event_id": "event-20"},
            now=NOW + timedelta(seconds=20),
        ).reason
        == "significant_event_20"
    )


def test_fallback_is_disabled_when_host_supplies_progress_signals():
    reducer = CheckpointReducer(host_progress_signals=True)
    for index in range(20):
        assert reducer.observe(
            {"type": "mutation", "changed": True},
            now=NOW + timedelta(seconds=index),
        ) is None


def test_repeated_reads_unchanged_status_and_duplicate_event_ids_do_not_count():
    reducer = CheckpointReducer(host_progress_signals=False)
    for index in range(30):
        assert reducer.observe({"type": "read", "event_id": f"read-{index}"}, now=NOW) is None
        assert reducer.observe({"type": "status", "changed": False}, now=NOW) is None
    for index in range(19):
        assert reducer.observe(
            {"type": "mutation", "changed": True, "event_id": f"write-{index}"}, now=NOW
        ) is None
    duplicate = {"type": "mutation", "changed": True, "event_id": "write-18"}
    assert reducer.observe(duplicate, now=NOW) is None
    assert reducer.observe(
        {"type": "mutation", "changed": True, "event_id": "write-20"}, now=NOW
    ).reason == "significant_event_20"


def test_reducer_state_round_trips_for_short_lived_adapters():
    reducer = CheckpointReducer(host_progress_signals=False)
    for index in range(7):
        reducer.observe({"type": "mutation", "changed": True}, now=NOW)
    restored = CheckpointReducer.from_state(reducer.to_state())
    for index in range(12):
        assert restored.observe({"type": "mutation", "changed": True}, now=NOW) is None
    assert restored.observe({"type": "mutation", "changed": True}, now=NOW).reason == "significant_event_20"


def test_handoff_is_bounded_and_contains_only_active_operational_fields():
    projection = ProjectProjection(
        project="demo",
        goal={"goal": "Ship Stage 2"},
        phase={"phase": "Implementation detail must not be injected"},
        current_task={"task": "Implement checkpoint triggers"},
        next_actions={f"next-{i}": f"Action {i}" for i in range(6)},
        decisions={f"decision-{i}": f"Decision {i}" for i in range(6)},
        blockers={"blocker-1": "Waiting for CI"},
        changed_files={"file": "secret implementation detail"},
        commands={"command": "dangerous command detail"},
        verification={"verify": "internal status"},
        last_applied_sequence=42,
    )

    handoff = build_handoff(projection, max_actions=10)

    assert len(handoff) <= 2400
    assert "Ship Stage 2" in handoff
    assert "Implement checkpoint triggers" in handoff
    assert sum(f"Action {i}" in handoff for i in range(6)) == 3
    assert "Waiting for CI" in handoff
    assert "Decision 5" in handoff
    assert "Implementation detail" not in handoff
    assert "secret implementation detail" not in handoff
    assert "dangerous command detail" not in handoff
    assert "internal status" not in handoff
    assert "project:demo" in handoff
    assert "sequence:42" in handoff


def test_handoff_hard_limit_applies_to_long_values():
    projection = ProjectProjection(
        project="demo",
        goal={"goal": "g" * 4000},
        current_task={"task": "t" * 4000},
        blockers={"blocker": "b" * 4000},
        last_applied_sequence=1,
    )
    handoff = build_handoff(projection, max_chars=300)
    assert len(handoff) <= 300
    assert "project:demo" in handoff
    assert "sequence:1" in handoff
