"""Capture entry points preserve accepted evidence through the v3 queue.

Producer regressions exercise real durable publication and worker completion;
content/time suppression is replaced by occurrence identity, not reimplemented.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import integration_adapter
import pytest
from llm_wiki import integration_events, integration_lifecycle

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.capture_hooks_helpers import (
    HOOKS,
    _deliver_hook,
    _hook_payload,
    _published_content,
    _run_capture_with_stdin,
)

# ---------------------------------------------------------------------------
# UserPromptSubmit capture — user_prompt_capture.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_state_file(tmp_path, monkeypatch):
    """Each test owns its prompt counter state."""
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "own-state"))


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
    import prompt_capture_followup

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    # Twenty threads contending for one file lock: the claim is that no update
    # is lost, not that each waits under ten seconds, and ten was not enough on
    # a loaded Windows runner.
    errors = []
    update_state = memory_state.update_state

    def observed_update(mutator, **kwargs):
        try:
            return update_state(mutator, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(memory_state, "update_state", observed_update)

    with ThreadPoolExecutor(max_workers=20) as pool:
        thresholds = list(
            pool.map(
                lambda _: prompt_capture_followup.increment_prompt_count(
                    "session-a", "project-a", lock_timeout=120.0
                ),
                range(100),
            )
        )

    state = json.loads(memory_state.state_file().read_text(encoding="utf-8"))
    assert errors == []
    assert state["user_prompt_counts"]["session-a"] == 100
    assert sum(count % 20 == 0 for count in thresholds) == 5


def test_prompt_counters_are_isolated_across_parallel_sessions(tmp_path, monkeypatch):
    import memory_state
    import prompt_capture_followup

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    work = [(session, project) for session, project in (("s-a", "p-a"), ("s-b", "p-b")) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        counts = list(pool.map(
            lambda item: prompt_capture_followup.increment_prompt_count(*item, lock_timeout=120.0), work
        ))

    state = json.loads(memory_state.state_file().read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"s-a": 20, "s-b": 20}
    assert counts.count(20) == 2


def test_prompt_counter_uses_project_fallback_for_missing_session(tmp_path, monkeypatch):
    import memory_state
    import prompt_capture_followup

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    assert prompt_capture_followup.increment_prompt_count("", "project-a") == 1
    assert prompt_capture_followup.increment_prompt_count("unknown", "project-a") == 2
    state = json.loads(memory_state.state_file().read_text(encoding="utf-8"))
    assert state["user_prompt_counts"] == {"project:project-a": 2}


def _default_state_lock_wait() -> float:
    """The lock wait a writer gets when it names none: what a hook must never pay."""
    import inspect

    import memory_state

    return inspect.signature(memory_state.update_state).parameters["lock_timeout"].default


def test_prompt_bookkeeping_reports_a_busy_state_without_the_normal_writer_wait(
    tmp_path, monkeypatch
):
    import memory_state
    import prompt_capture_followup

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    started = time.perf_counter()
    with pytest.raises(memory_state.StateLockTimeout):
        prompt_capture_followup.increment_prompt_count("session-a", "project-a")
    count_elapsed = time.perf_counter() - started

    assert count_elapsed < _default_state_lock_wait()


# The twentieth prompt is pinned by
# `tests/test_the_twentieth_prompt_captures_the_session.py`, with a real counter.


def test_short_advisory_refresh_includes_page_and_stale_counts(tmp_path, monkeypatch):
    import build_advisory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    long_ago = time.time() - 120 * 86400
    for index in range(4):
        page = notes / f"page-{index}.md"
        page.write_text("---\ntype: concept\n---\n", encoding="utf-8")
        if index < 2:
            os.utime(page, (long_ago, long_ago))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

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
    from operational_ownership import OperationalOwnershipError
    from project_journal import ProjectJournalReadError, ProjectJournalRebuildRequired
    from queue_core import (
        QueueOperationError,
    )

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


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_preserves_short_meaningful_actions(ingress, module_name, payload):
    rc = _run_capture_with_stdin(module_name, _hook_payload(payload))
    stored = json.loads(_published_content(ingress[0]))
    assert rc == 0
    assert stored["session"] == "direct-session"
    assert _deliver_hook(ingress) is not None
    assert (ingress[0].vault / "knowledge/daily/2026-09-29.md").is_file()


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_replay_after_commit_writes_one_record(ingress, module_name, payload):
    hook = _hook_payload(payload)
    _run_capture_with_stdin(module_name, hook)
    assert _deliver_hook(ingress) is not None
    _run_capture_with_stdin(module_name, hook)
    assert _deliver_hook(ingress) is None
    journal = ingress[0].vault / "knowledge/daily/2026-09-29.md"
    assert journal.read_bytes().count(b"<!-- llm-wiki-operation:") == 1


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_keeps_distinct_host_occurrences(ingress, module_name, payload):
    _run_capture_with_stdin(module_name, _hook_payload(payload, "first"))
    _run_capture_with_stdin(module_name, _hook_payload(payload, "second"))
    queue = ingress[0]
    assert queue.claim_capture("one", handler_versions=(2,)) is not None
    assert queue.claim_capture("two", handler_versions=(2,)) is not None
    assert queue.claim_capture("three", handler_versions=(2,)) is None


@pytest.mark.parametrize('module_name,payload', HOOKS)
def test_direct_capture_is_durable_while_another_process_holds_the_journal_writer(
    ingress, module_name, payload,
):
    from tests.capture_publication_with_busy_writer_helpers import _other_process_writer

    queue = ingress[0]
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

    queue, _coordinator = ingress
    original = breadcrumb_storage.publish_breadcrumb
    followed = []

    def unavailable(*args, **kwargs):
        raise OSError("disk full before durable acceptance")

    monkeypatch.setattr(integration_lifecycle, "after_breadcrumb", lambda *args: followed.append(True))
    monkeypatch.setattr(breadcrumb_storage, "publish_breadcrumb", unavailable)
    assert _run_capture_with_stdin(module_name, _hook_payload(payload)) == 0
    assert (followed, queue.claim_capture("failed", handler_versions=(2,))) == ([], None)
    monkeypatch.setattr(breadcrumb_storage, "publish_breadcrumb", original)
    _run_capture_with_stdin(module_name, _hook_payload(payload))
    assert queue.claim_capture("retry", handler_versions=(2,)) is not None


@pytest.mark.parametrize("module_name,payload", HOOKS)
def test_direct_capture_survives_exit_after_acceptance(ingress, monkeypatch, module_name, payload):
    original = integration_adapter.after_breadcrumb

    def interrupted(*args):
        raise SystemExit(86)

    monkeypatch.setattr(integration_lifecycle, "after_breadcrumb", interrupted)
    with pytest.raises(SystemExit, match="86"):
        _run_capture_with_stdin(module_name, _hook_payload(payload))
    monkeypatch.setattr(integration_lifecycle, "after_breadcrumb", original)
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
    content = _published_content(ingress[0])
    assert b"sk-abcdefghijklmnopqrstuvwxyz012345" not in content
    assert b"[REDACTED]" in content


def test_rejected_envelope_has_no_publication_or_followups(ingress, monkeypatch):
    queue, _coordinator = ingress
    followed = []

    def reject(**kwargs):
        raise ValueError("invalid event payload")

    monkeypatch.setattr(integration_events, "build_event_envelope", reject)
    monkeypatch.setattr(integration_lifecycle, "after_breadcrumb", lambda *args: followed.append(True))
    _run_capture_with_stdin("user_prompt_capture", _hook_payload({"prompt": "reject"}))
    assert followed == []
    assert queue.claim_capture("rejected", handler_versions=(2,)) is None
