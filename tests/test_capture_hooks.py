"""Capture entry points preserve accepted evidence through the v3 queue.

Producer regressions exercise real durable publication and worker completion;
content/time suppression is replaced by occurrence identity, not reimplemented.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_breadcrumb_storage import _bundle
from tests.test_breadcrumb_worker import ingress as ingress

# ---------------------------------------------------------------------------
# UserPromptSubmit capture — user_prompt_capture.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_state_file(tmp_path, monkeypatch):
    """Each test owns its prompt counter state."""
    import memory_state

    run = tmp_path / "own-state"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")


def _run_capture_with_stdin(module_name: str, stdin_payload: dict | str) -> int:
    """Helper: invoke capture script's main() with simulated stdin."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    mod = __import__(module_name)

    # Simulate stdin
    if isinstance(stdin_payload, dict):
        stdin_text = json.dumps(stdin_payload)
    else:
        stdin_text = stdin_payload

    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return mod.main()


def test_prompt_capture_exits_zero_on_empty_stdin():
    """No stdin → no crash, exit 0."""
    rc = _run_capture_with_stdin("user_prompt_capture", "")
    assert rc == 0


def test_prompt_capture_exits_zero_on_malformed_json():
    """Garbage stdin → no crash, exit 0."""
    rc = _run_capture_with_stdin("user_prompt_capture", "not even json {{{")
    assert rc == 0


def test_prompt_counter_is_durable_and_concurrency_safe(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    # Twenty threads contending for one file lock: the claim is that no update
    # is lost, not that each waits under ten seconds, and ten was not enough on
    # a loaded Windows runner.
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 120.0)
    errors = []

    def observed_update(mutator, **kwargs):
        try:
            return memory_state.update_state(mutator, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(user_prompt_capture, "update_state", observed_update)

    with ThreadPoolExecutor(max_workers=20) as pool:
        thresholds = list(
            pool.map(
                lambda _: user_prompt_capture._increment_prompt_count("session-a", "project-a"),
                range(100),
            )
        )

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert errors == []
    assert state["user_prompt_counts"]["session-a"] == 100
    assert sum(count % 20 == 0 for count in thresholds) == 5


def test_prompt_counters_are_isolated_across_parallel_sessions(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)
    monkeypatch.setattr(user_prompt_capture, "HOOK_STATE_LOCK_TIMEOUT", 120.0)

    work = [(session, project) for session, project in (("s-a", "p-a"), ("s-b", "p-b")) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        counts = list(pool.map(lambda item: user_prompt_capture._increment_prompt_count(*item), work))

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"s-a": 20, "s-b": 20}
    assert counts.count(20) == 2


def test_prompt_counter_uses_project_fallback_for_missing_session(tmp_path, monkeypatch):
    import memory_state
    import user_prompt_capture

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)

    assert user_prompt_capture._increment_prompt_count("", "project-a") == 1
    assert user_prompt_capture._increment_prompt_count("unknown", "project-a") == 2
    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"project:project-a": 2}


def _default_state_lock_wait(memory_state) -> float:
    """The lock wait a writer gets when it names none: what a hook must never pay."""
    import inspect

    return inspect.signature(memory_state.update_state).parameters["lock_timeout"].default


def test_prompt_bookkeeping_reports_a_busy_state_without_the_normal_writer_wait(
    tmp_path, monkeypatch
):
    import memory_state
    import user_prompt_capture

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock_file)
    monkeypatch.setattr(user_prompt_capture, "update_state", memory_state.update_state)

    started = time.perf_counter()
    with pytest.raises(memory_state.StateLockTimeout):
        user_prompt_capture._increment_prompt_count("session-a", "project-a")
    count_elapsed = time.perf_counter() - started

    assert count_elapsed < _default_state_lock_wait(memory_state)


# The twentieth prompt is pinned by
# `tests/test_the_twentieth_prompt_captures_the_session.py`, with a real counter.


def test_short_advisory_refresh_includes_page_and_stale_counts(tmp_path, monkeypatch):
    import build_advisory

    notes = tmp_path / "notes"
    notes.mkdir()
    for index in range(4):
        (notes / f"page-{index}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8")
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_advisory, "_find_stale_pages", lambda: 2)

    refresh = build_advisory.build_advisory_refresh()

    assert "4 pages" in refresh
    assert "2 stale" in refresh
    assert len(refresh.split()) <= 50


# ---------------------------------------------------------------------------
# PostToolUse capture — post_tool_capture.py
# ---------------------------------------------------------------------------


def test_tool_capture_exits_zero_on_empty_stdin():
    rc = _run_capture_with_stdin("post_tool_capture", "")
    assert rc == 0


def test_operational_errors_survive_a_process_boundary():
    """These cross the queue worker's process boundary; they must arrive intact.

    A `BlackboardConflictError` in a worker child used to reach the parent as
    `TypeError: __init__() missing 1 required positional argument`, hiding the
    real failure.
    """
    import pickle

    from blackboard import BlackboardConflictError
    from markdown_transaction import (
        ProjectPendingPriorError,
        TransactionDriftError,
        TransactionFailure,
    )
    from memory_queue import QueueOperationError
    from operational_ownership import OperationalOwnershipError
    from project_journal import ProjectJournalReadError, ProjectJournalRebuildRequired

    errors = [
        BlackboardConflictError(("resource-a",), "conflict-1"),
        TransactionFailure("boom", "target_drift", "committed"),
        TransactionDriftError("tx-1", ("knowledge/notes/a.md",)),
        ProjectPendingPriorError("demo", 4, 3),
        ProjectJournalRebuildRequired("demo", 4, 2),
        ProjectJournalReadError("unreadable", "file is not regular"),
        QueueOperationError("process_cleanup_failed", "child exited 1"),
        OperationalOwnershipError("owner_busy", "another owner holds the lease"),
    ]

    for error in errors:
        restored = pickle.loads(pickle.dumps(error))
        assert type(restored) is type(error)
        assert str(restored) == str(error)
        assert vars(restored) == vars(error)


HOOKS = (("user_prompt_capture", {"prompt": "да"}),
         ("post_tool_capture", {"tool_name": "Bash", "tool_input": {"command": "pwd"}}))


def _hook_payload(payload, event_id="host-once"):
    return {**payload, "event_id": event_id, "session_id": "direct-session",
            "timestamp": "2026-09-29T12:00:00+00:00", "cwd": "/fixture"}


def _published_content(queue):
    ready = list((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
    assert len(ready) == 1
    identity = json.loads(ready[0].read_bytes())["intent_id"]
    return _bundle(queue.state_root, identity).content


def _deliver_hook(ingress):
    from functools import partial

    import flush_memory

    _adapter, queue, coordinator = ingress
    return flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_preserves_short_meaningful_actions(ingress, module_name, payload):
    rc = _run_capture_with_stdin(module_name, _hook_payload(payload))
    stored = json.loads(_published_content(ingress[1]))
    assert rc == 0
    assert stored["session"] == "direct-session"
    assert _deliver_hook(ingress) is not None
    assert (ingress[1].vault / "knowledge/daily/2026-09-29.md").is_file()


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_replay_after_commit_writes_one_record(ingress, module_name, payload):
    hook = _hook_payload(payload)
    _run_capture_with_stdin(module_name, hook)
    assert _deliver_hook(ingress) is not None
    _run_capture_with_stdin(module_name, hook)
    assert _deliver_hook(ingress) is None
    journal = ingress[1].vault / "knowledge/daily/2026-09-29.md"
    assert journal.read_bytes().count(b"<!-- llm-wiki-operation:") == 1


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_keeps_distinct_host_occurrences(ingress, module_name, payload):
    _run_capture_with_stdin(module_name, _hook_payload(payload, "first"))
    _run_capture_with_stdin(module_name, _hook_payload(payload, "second"))
    queue = ingress[1]
    assert queue.claim_capture("one", handler_versions=(2,)) is not None
    assert queue.claim_capture("two", handler_versions=(2,)) is not None
    assert queue.claim_capture("three", handler_versions=(2,)) is None


@pytest.mark.parametrize('module_name,payload', HOOKS)
def test_direct_capture_is_durable_while_another_process_holds_the_journal_writer(
    ingress, module_name, payload,
):
    from tests.test_capture_publication_with_busy_writer import _other_process_writer

    queue = ingress[1]
    with _other_process_writer(queue.state_root):
        _run_capture_with_stdin(module_name, _hook_payload(payload))
        assert json.loads(_published_content(queue))['session'] == 'direct-session'
        assert not (queue.vault / 'knowledge/daily/2026-09-29.md').exists()
    assert _deliver_hook(ingress) is not None
    assert (queue.vault / 'knowledge/daily/2026-09-29.md').is_file()


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_retries_publication_failure_without_followups(
    ingress, monkeypatch, module_name, payload,
):
    import breadcrumb_storage

    adapter, queue, _coordinator = ingress
    original = breadcrumb_storage.publish_breadcrumb
    followed = []

    def unavailable(*args, **kwargs):
        raise OSError("disk full before durable acceptance")

    monkeypatch.setattr(adapter, "_after_breadcrumb", lambda *args: followed.append(True))
    monkeypatch.setattr(breadcrumb_storage, "publish_breadcrumb", unavailable)
    assert _run_capture_with_stdin(module_name, _hook_payload(payload)) == 0
    assert (followed, queue.claim_capture("failed", handler_versions=(2,))) == ([], None)
    monkeypatch.setattr(breadcrumb_storage, "publish_breadcrumb", original)
    _run_capture_with_stdin(module_name, _hook_payload(payload))
    assert queue.claim_capture("retry", handler_versions=(2,)) is not None


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_survives_exit_after_acceptance(ingress, monkeypatch, module_name, payload):
    adapter = ingress[0]
    original = adapter._after_breadcrumb

    def interrupted(*args):
        raise SystemExit(86)

    monkeypatch.setattr(adapter, "_after_breadcrumb", interrupted)
    with pytest.raises(SystemExit, match="86"):
        _run_capture_with_stdin(module_name, _hook_payload(payload))
    monkeypatch.setattr(adapter, "_after_breadcrumb", original)
    _run_capture_with_stdin(module_name, _hook_payload(payload))
    assert _deliver_hook(ingress) is not None
    assert _deliver_hook(ingress) is None


@pytest.mark.parametrize("module_name,payload", [
    ("user_prompt_capture", {"prompt": "token=sk-abcdefghijklmnopqrstuvwxyz012345"}),
    ("post_tool_capture", {"tool_name": "Bash", "tool_input": {
        "command": "echo token=sk-abcdefghijklmnopqrstuvwxyz012345"}}),
])
def test_direct_capture_redacts_before_durable_storage(ingress, module_name, payload):
    _run_capture_with_stdin(module_name, _hook_payload(payload))
    content = _published_content(ingress[1])
    assert b"sk-abcdefghijklmnopqrstuvwxyz012345" not in content
    assert b"[REDACTED]" in content


def test_rejected_envelope_has_no_publication_or_followups(ingress, monkeypatch):
    adapter, queue, _coordinator = ingress
    followed = []

    def reject(**kwargs):
        raise ValueError("invalid event payload")

    monkeypatch.setattr(adapter, "build_event_envelope", reject)
    monkeypatch.setattr(adapter, "_after_breadcrumb", lambda *args: followed.append(True))
    _run_capture_with_stdin("user_prompt_capture", _hook_payload({"prompt": "reject"}))
    assert followed == []
    assert queue.claim_capture("rejected", handler_versions=(2,)) is None
