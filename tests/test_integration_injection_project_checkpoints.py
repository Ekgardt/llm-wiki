"""Project checkpoints from lifecycle events: journaled exactly once, debounced in order, batched across failure and restart, and recovered before the session-start handoff."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import integration_adapter
import pytest
from llm_wiki import (
    event_project,
    integration_session_start,
)

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_case_helpers import (
    _unmet_substrings,
)
from tests.integration_injection_helpers import (
    ROOT,
    _ensure_scripts_on_path,
)


def _session_end_events(project_dir: Path, count: int) -> list:
    return [
        integration_adapter.normalize_event(
            "codex",
            "session_end",
            {
                "session_id": "session-1",
                "cwd": str(project_dir),
                "event_id": f"event-{index}",
            },
        )
        for index in range(count)
    ]


def _journal_records(journal: str, header: str) -> list[dict]:
    lines = journal.removeprefix(header).splitlines()
    return [json.loads(line) for line in lines if line]


def test_concurrent_distinct_events_are_each_journaled_exactly_once(monkeypatch, tmp_path):
    _ensure_scripts_on_path()
    import project_checkpoint
    from project_journal import JOURNAL_HEADER, ProjectStore

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project_dir = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    project_dir.mkdir()
    runtime_state = {}
    state_lock = threading.Lock()

    def update(mutator, **kwargs):
        with state_lock:
            mutator(runtime_state)
            return runtime_state

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(
        event_project, "project_context", lambda event: ("demo", project_dir)
    )
    events = _session_end_events(project_dir, 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(project_checkpoint.observe_project_checkpoint, events))

    journal = ProjectStore(vault, state_root).read_journal("demo")
    records = _journal_records(journal, JOURNAL_HEADER)
    assert sorted(record["occurrence_id"] for record in records) == sorted(
        project_checkpoint.batch_occurrence_id([event.event_id]) for event in events
    )
    assert len(records) == 2


def test_project_lease_busy_event_remains_pending_until_next_observation(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint
    from project_journal import ProjectLeaseBusy

    state = {}
    attempts = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner):
            attempts.append(event["occurrence_id"])
            if len(attempts) == 1:
                raise ProjectLeaseBusy("busy")

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    first = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "one"}
    )
    second = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "two"}
    )

    with pytest.raises(ProjectLeaseBusy):
        project_checkpoint.observe_project_checkpoint(first)
    pending = state["project_checkpoint_pending"]["demo"]
    assert [item["event_id"] for item in pending] == [first.event_id]

    project_checkpoint.observe_project_checkpoint(second)
    # The retry of a batch must reuse the batch's name; that is what makes the
    # coordinator collapse it instead of refusing it.
    assert attempts == [
        project_checkpoint.batch_occurrence_id([first.event_id]),
        project_checkpoint.batch_occurrence_id([first.event_id]),
        project_checkpoint.batch_occurrence_id([second.event_id]),
    ]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_reducer_commit_failure_releases_pending_claim_for_retry(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    state = {}
    updates = 0
    checkpoints = []

    def update(mutator, **kwargs):
        nonlocal updates
        updates += 1
        # claim, in-flight record (2026-09-14), journal, then this commit
        if updates == 4:
            raise TimeoutError("commit state busy")
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner):
            checkpoints.append(event["occurrence_id"])

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    event = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "one"}
    )

    with pytest.raises(TimeoutError, match="commit state busy"):
        project_checkpoint.observe_project_checkpoint(event)
    pending = state["project_checkpoint_pending"]["demo"][0]
    assert "claim_owner" not in pending

    project_checkpoint.observe_project_checkpoint(event)
    assert checkpoints == [
        project_checkpoint.batch_occurrence_id([event.event_id]),
        project_checkpoint.batch_occurrence_id([event.event_id]),
    ]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_session_start_maintenance_does_not_debounce_or_drop_following_delta(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint
    from project_journal import CheckpointReducer

    previous = datetime.fromisoformat("2026-07-13T11:59:29+00:00")
    session_start_at = datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    ordinary_event_at = datetime.fromisoformat("2026-07-13T12:00:01+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=previous).to_state()
        }
    }
    checkpoints = []
    delta = project_checkpoint.empty_delta()
    delta["current_task"] = {
        "id": "task-1",
        "action": "upsert",
        "value": "Preserve this delta",
    }

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    start = integration_adapter.normalize_event(
        "claude",
        "session_start",
        {"session_id": "s1", "cwd": "C:/project", "event_id": "start-1"},
        occurred_at=session_start_at,
    )
    ordinary = integration_adapter.normalize_event(
        "claude",
        "post_tool_use",
        {
            "session_id": "s1",
            "cwd": "C:/project",
            "event_id": "event-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
            "checkpoint_type": "correction",
            "project_delta": delta,
        },
        occurred_at=ordinary_event_at,
    )

    project_checkpoint.observe_project_checkpoint(start)
    project_checkpoint.observe_project_checkpoint(ordinary)

    reducer_state = state["project_checkpoint_reducers"]["demo:s1"]
    assert (
        reducer_state["last_checkpoint_at"],
        state["project_checkpoint_pending"]["demo"],
        len(checkpoints),
        checkpoints[0]["occurrence_id"],
        checkpoints[0]["delta"]["current_task"],
        checkpoints[0]["delta"]["current_task_operations"],
    ) == (
        "2026-07-13T12:00:01.000000Z",
        [],
        1,
        project_checkpoint.batch_occurrence_id([ordinary.event_id]),
        delta["current_task"],
        [delta["current_task"]],
    )


def test_debounced_deltas_flush_in_order_on_later_observation_exactly_once(monkeypatch):

    _ensure_scripts_on_path()
    import project_checkpoint
    from project_journal import CheckpointReducer

    start = datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=start).to_state()
        }
    }
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    def delta(**changes):
        value = project_checkpoint.empty_delta()
        value.update(changes)
        return value

    def event(event_id, seconds, checkpoint_type=None, project_delta=None):
        return _read_tool_event(start + timedelta(seconds=seconds),
            event_id,
            checkpoint_type=checkpoint_type,
            project_delta=project_delta,
        )

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda envelope: ("demo", ROOT))
    correction = event(
        "correction-1",
        1,
        "correction",
        delta(current_task={"id": "task-1", "action": "upsert", "value": "First"}),
    )
    blocker = event(
        "blocker-1",
        2,
        "blocker_opened",
        delta(blockers=[{"id": "blocker-1", "action": "upsert", "value": "Waiting"}]),
    )
    file_change = event(
        "file-1",
        3,
        "file_changed",
        delta(
            current_task={"id": "task-1", "action": "upsert", "value": "Latest"},
            blockers=[{"id": "blocker-1", "action": "close", "value": "Resolved"}],
            changed_files=[{"id": "src/app.py", "action": "upsert", "value": "src/app.py"}],
        ),
    )
    timer = event("timer-1", 31)

    project_checkpoint.observe_project_checkpoint(correction)
    project_checkpoint.observe_project_checkpoint(blocker)
    project_checkpoint.observe_project_checkpoint(file_change)
    assert (checkpoints, _pending_event_ids(state)) == (
        [],
        [correction.event_id, blocker.event_id, file_change.event_id],
    )

    state = json.loads(json.dumps(state))
    project_checkpoint.observe_project_checkpoint(timer)
    assert len(checkpoints) == 1
    merged = checkpoints[0]
    # The timer is a later observation of a real tool, and since 2026-09-05 an
    # observation reaches the journal without the agent narrating a delta for
    # it. So the newest current task is what the timer saw, not the last
    # narrated value — while the order of everything narrated before it, and
    # the exactly-once flush this test exists for, are unchanged.
    assert (
        merged["delta"]["current_task"]["value"].startswith("Read src/app.py"),
        [operation["value"] for operation in merged["delta"]["current_task_operations"]][:2],
        merged["delta"]["blockers"],
        merged["delta"]["changed_files"],
        merged["evidence_event_ids"],
        state["project_checkpoint_pending"]["demo"],
    ) == (
        True,
        ["First", "Latest"],
        [{"id": "blocker-1", "action": "close", "value": "Resolved"}],
        [{"id": "src/app.py", "action": "upsert", "value": "src/app.py"}],
        [correction.event_id, blocker.event_id, file_change.event_id, timer.event_id],
        [],
    )

    state = json.loads(json.dumps(state))
    project_checkpoint.observe_project_checkpoint(timer)
    assert len(checkpoints) == 1


def test_bypass_event_immediately_flushes_debounced_delta_once_after_restart(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint
    from project_journal import CheckpointReducer

    start = datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=start).to_state()
        }
    }
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    def make_event(event_id, seconds, checkpoint_type, field, operation):
        project_delta = project_checkpoint.empty_delta()
        project_delta[field] = operation
        return integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "session_id": "s1",
                "cwd": "C:/project",
                "event_id": event_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "checkpoint_type": checkpoint_type,
                "project_delta": project_delta,
            },
            occurred_at=start + timedelta(seconds=seconds),
        )

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda envelope: ("demo", ROOT))
    correction = make_event(
        "correction-1",
        1,
        "correction",
        "current_task",
        {"id": "task-1", "action": "upsert", "value": "Keep me"},
    )
    decision = make_event(
        "decision-1",
        2,
        "decision",
        "decisions",
        [{"id": "decision-1", "action": "upsert", "value": "Flush now"}],
    )

    project_checkpoint.observe_project_checkpoint(correction)
    assert checkpoints == []
    project_checkpoint.observe_project_checkpoint(decision)

    assert (
        len(checkpoints),
        checkpoints[0]["reason"],
        checkpoints[0]["delta"]["current_task"]["value"],
        checkpoints[0]["delta"]["decisions"][0]["value"],
        checkpoints[0]["evidence_event_ids"],
        state["project_checkpoint_pending"]["demo"],
    ) == (
        1,
        "decision",
        "Keep me",
        "Flush now",
        [correction.event_id, decision.event_id],
        [],
    )

    state = json.loads(json.dumps(state))
    project_checkpoint.observe_project_checkpoint(decision)
    assert len(checkpoints) == 1


def _pending_delta(index: int) -> dict:
    import project_checkpoint
    delta = project_checkpoint.empty_delta()
    delta["current_task"] = {
        "id": f"task-{index}",
        "action": "upsert",
        "value": f"Task {index}",
    }
    delta["decisions"] = [
        {"id": f"decision-{index}", "action": "upsert", "value": f"Decision {index}"}
    ]
    return delta


def _batch_checkpoint_type(index: int, last: int) -> str:
    if index == last:
        return "decision"
    return "correction"


def _batch_offset_seconds(index: int, last: int) -> int:
    if index == last:
        return 2
    return 1


def _seed_pending_checkpoints(state: dict, started, total: int) -> list[str]:
    import project_checkpoint
    last = total - 1
    event_ids: list[str] = []
    for index in range(total):
        envelope = integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "session_id": "s1",
                "cwd": "C:/project",
                "event_id": f"event-{index}",
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "checkpoint_type": _batch_checkpoint_type(index, last),
                "project_delta": _pending_delta(index),
            },
            occurred_at=started + timedelta(seconds=_batch_offset_seconds(index, last)),
        )
        event_ids.append(envelope.event_id)
        state["project_checkpoint_pending"]["demo"].append(
            project_checkpoint.pending_checkpoint(envelope, "demo", "demo:s1")
        )
    return event_ids


def _evidence_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["evidence_event_ids"]) for item in checkpoints]


def _task_operation_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["delta"]["current_task_operations"]) for item in checkpoints]


def _decision_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["delta"]["decisions"]) for item in checkpoints]


def _flat_evidence_ids(checkpoints: list[dict]) -> list[str]:
    return [event_id for item in checkpoints for event_id in item["evidence_event_ids"]]


def test_bypass_flush_batches_205_pending_events_across_failure_and_restart(monkeypatch):
    _ensure_scripts_on_path()
    import project_checkpoint
    from project_journal import CheckpointReducer

    started = datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=started).to_state()
        },
        "project_checkpoint_pending": {"demo": []},
    }
    attempts = 0
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("second batch interrupted")
            checkpoints.append(event)

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    event_ids = _seed_pending_checkpoints(state, started, 205)

    with pytest.raises(RuntimeError, match="second batch interrupted"):
        project_checkpoint.drain_project_checkpoints("demo", "demo")
    assert (
        len(checkpoints),
        len(checkpoints[0]["evidence_event_ids"]),
        len(state["project_checkpoint_pending"]["demo"]),
    ) == (
        1,
        100,
        105,
    )

    state = json.loads(json.dumps(state))
    project_checkpoint.drain_project_checkpoints("demo", "demo")

    assert (
        _evidence_lengths(checkpoints),
        _task_operation_lengths(checkpoints),
        max(_decision_lengths(checkpoints)) <= 100,
        _flat_evidence_ids(checkpoints),
        state["project_checkpoint_pending"]["demo"],
    ) == (
        [100, 100, 5],
        [100, 100, 5],
        True,
        event_ids,
        [],
    )

    state = json.loads(json.dumps(state))
    project_checkpoint.drain_project_checkpoints("demo", "demo")
    assert len(checkpoints) == 3


def test_single_oversized_valid_delta_splits_before_enqueue(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    state = {}
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    delta = project_checkpoint.empty_delta()
    delta["decisions"] = [
        {"id": f"decision-{index}", "action": "upsert", "value": str(index)} for index in range(205)
    ]
    envelope = integration_adapter.normalize_event(
        "claude",
        "post_tool_use",
        {
            "session_id": "s1",
            "cwd": "C:/project",
            "event_id": "oversized-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
            "checkpoint_type": "decision",
            "project_delta": delta,
        },
    )
    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))

    project_checkpoint.observe_project_checkpoint(envelope)

    assert [len(item["delta"]["decisions"]) for item in checkpoints] == [100, 100, 5]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_session_start_recovers_transactions_then_project_before_handoff(
    monkeypatch, tmp_path, capsys
):
    import io

    _ensure_scripts_on_path()
    import session_start_project_state
    from project_journal import ProjectProjection

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    (vault / "knowledge/projects/demo/journal.md").write_text("journal", encoding="utf-8")
    project.mkdir()
    calls = []

    class Coordinator:
        def recover(self):
            calls.append("transactions")

    class Store:
        def __init__(self, vault_root, state_root):
            self.coordinator = Coordinator()

        def recover(self, slug, **kwargs):
            self.coordinator.recover()
            calls.append(("project", slug))

        def projection(self, slug):
            calls.append(("projection", slug))
            return ProjectProjection(project=slug, last_applied_sequence=7)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(session_start_project_state, "compute_slug", lambda *args: "demo")
    monkeypatch.setattr(session_start_project_state, "ProjectStore", Store, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert session_start_project_state.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        "transactions",
        ("project", "demo"),
        ("projection", "demo"),
    ]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "project:demo" in context
    assert "sequence:7" in context


def test_session_start_recovers_interrupted_first_checkpoint_before_journal_exists(
    monkeypatch, tmp_path, capsys
):

    _ensure_scripts_on_path()
    import session_start_project_state
    from project_journal import ProjectProjection

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    project.mkdir()
    project_state = vault / "knowledge/projects/demo"
    project_state.mkdir(parents=True)
    calls = []

    class Coordinator:
        def recover(self):
            calls.append("transactions")

    class Store:
        def __init__(self, vault_root, state_root):
            self.coordinator = Coordinator()

        def recover(self, slug, **kwargs):
            self.coordinator.recover()
            calls.append(("project", slug))
            (project_state / "journal.md").write_text("recovered", encoding="utf-8")

        def projection(self, slug):
            calls.append(("projection", slug))
            return ProjectProjection(project=slug, last_applied_sequence=1)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(session_start_project_state, "compute_slug", lambda *args: "demo")
    monkeypatch.setattr(session_start_project_state, "ProjectStore", Store)
    assert (
        not (project_state / "journal.md").exists(),
        session_start_project_state.main(),
    ) == (
        True,
        0,
    )
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        "transactions",
        ("project", "demo"),
        ("projection", "demo"),
    ]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert (
        _unmet_substrings(
            (
                ("project:demo", context, True),
                ("sequence:1", context, True),
            )
        )
        == []
    )


def _pending_event_ids(state: dict) -> list[str]:
    return [item["event_id"] for item in state["project_checkpoint_pending"]["demo"]]


def _read_tool_event(occurred_at, event_id, *, checkpoint_type=None, project_delta=None):
    """A Claude `Read` observation, carrying only the signals the test names."""
    raw = {
        "session_id": "s1",
        "cwd": "C:/project",
        "event_id": event_id,
        "tool_name": "Read",
        "tool_input": {"file_path": "src/app.py"},
    }
    optional = {"checkpoint_type": checkpoint_type, "project_delta": project_delta}
    raw.update({key: value for key, value in optional.items() if value})
    return integration_adapter.normalize_event("claude", "post_tool_use", raw, occurred_at=occurred_at)
