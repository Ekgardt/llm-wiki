"""A busy Markdown writer cannot consume an accepted breadcrumb."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial

import flush_memory
import integration_adapter
import iso_time
import post_tool_capture
import pytest
import user_prompt_capture
from markdown_transaction import active_markdown_coordinator
from memory_queue import active_memory_queue

from tests.adopted_capture_vault import adopted_capture_vault, published_intents
from tests.slow_machine import LONG_TIMEOUT


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    state_root, _ = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda *a, **k: None)
    monkeypatch.setattr(
        flush_memory,
        "_call_capture_classifier",
        lambda *a: pytest.fail("LLM called for a breadcrumb"),
    )
    root = integration_adapter.ROOT
    return (
        root,
        state_root,
        active_memory_queue(root, state_root),
        active_markdown_coordinator(root, state_root),
    )


def _work(delivery):
    _, _, queue, coordinator = delivery
    processor = partial(flush_memory.process_new_capture, queue, coordinator)
    return flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)


def _publish_prompt():
    from breadcrumb_capture import queue_breadcrumb

    return queue_breadcrumb("user_prompt", "demo", "session-a", {"preview": "original"}, "one")


def _pending_intents(state_root):
    return sorted((state_root / "run/capture-intents/pending").glob("*/*.json"))


def test_breadcrumb_retention_does_not_wait_for_a_database(delivery, monkeypatch):
    import markdown_transaction

    opened = []
    original = markdown_transaction.active_markdown_coordinator

    def open_coordinator(*args):
        coordinator = original(*args)
        opened.append(coordinator)
        return coordinator

    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", open_coordinator)
    assert _publish_prompt()
    assert opened == []
    assert len(_pending_intents(delivery[1])) == 1
    _work(delivery)
    assert len(published_intents(delivery[1])) == 1
    assert _pending_intents(delivery[1]) == []
    assert "original" in next((delivery[0] / "knowledge/daily").glob("*.md")).read_text()


def test_invalid_coordinator_cannot_publish_a_retained_breadcrumb(delivery, monkeypatch):
    import markdown_transaction

    def invalid(*args):
        raise ValueError("coordinator validation failed")

    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", invalid)
    assert _publish_prompt()
    with pytest.raises(ValueError, match="coordinator validation failed"):
        integration_adapter._run_active_capture_worker_once()
    assert published_intents(delivery[1]) == []
    assert len(_pending_intents(delivery[1])) == 1


def test_state_recovery_keeps_the_same_native_operation_id(monkeypatch):
    import capture_operation

    monkeypatch.setattr(capture_operation, "_count_dropped_write", lambda error: None)

    def unavailable(mutate):
        raise OSError("state lock unavailable")

    options = dict(
        namespace="prompt",
        key="same-content",
        prefix="user-prompt",
        source_event_id="native-event",
        rate_limit_seconds=30,
        max_entries=100,
        now=datetime.now(),
    )
    first = capture_operation.claim_operation(unavailable, **options)
    retry = capture_operation.claim_operation(lambda mutate: mutate({}), **options)
    assert first == retry


def test_replay_after_midnight_keeps_original_time_and_only_one_append(delivery, monkeypatch):
    root, state_root, _, _ = delivery
    before = datetime.fromisoformat("2026-09-28T23:59:50+03:00")
    after = datetime.fromisoformat("2026-09-30T01:00:00+03:00")
    monkeypatch.setattr(iso_time, "local_now", lambda: before)
    _publish_prompt()
    original = _pending_intents(state_root)[0].read_bytes()
    monkeypatch.setattr(iso_time, "local_now", lambda: after)
    _publish_prompt()
    _work(delivery)
    _publish_prompt()
    assert (_work(delivery), published_intents(state_root)[0].read_bytes()) == (None, original)
    daily = root / "knowledge/daily/2026-09-28.md"
    assert (daily.read_text().count("original"), "[23:59:50]" in daily.read_text()) == (1, True)


def test_ready_intent_survives_failed_dispatch_and_is_adopted(delivery, monkeypatch):
    from capture_adoption import complete_pending_capture_intents
    from memory_queue import _QueueV3CandidateReader

    root, state_root, _, _ = delivery
    original = _QueueV3CandidateReader.enqueue_capture_task_replay_safe

    def fail_dispatch(*args, **kwargs):
        raise OSError("publisher stopped after writing the ready intent")

    monkeypatch.setattr(_QueueV3CandidateReader, "enqueue_capture_task_replay_safe", fail_dispatch)
    assert _publish_prompt()
    outcome = complete_pending_capture_intents(delivery[2], delivery[3], state_root=state_root)
    assert len(outcome["skipped"]) == 1
    assert len(published_intents(state_root)) == 1
    monkeypatch.setattr(_QueueV3CandidateReader, "enqueue_capture_task_replay_safe", original)
    _work(delivery)
    assert "original" in next((root / "knowledge/daily").glob("*.md")).read_text()


def test_failure_before_retention_is_not_acknowledged(delivery, monkeypatch):
    def fail_before_publish(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("reliable_memory.publish_runtime_file", fail_before_publish)
    with pytest.raises(OSError, match="disk unavailable"):
        _publish_prompt()
    assert published_intents(delivery[1]) == []


def _expire_publisher_lease(delivery, monkeypatch):
    from operational_ownership import OwnershipRegistry

    with sqlite3.connect(delivery[3].database_path) as database:
        expires = database.execute(
            "SELECT expires_at FROM maintenance_owners WHERE role='capture'"
        ).fetchone()[0]
    after = datetime.fromisoformat(expires.replace("Z", "+00:00")) + timedelta(seconds=1)
    original = OwnershipRegistry._from_adopted_database.__func__

    def open_after_expiry(cls, *args, **kwargs):
        return original(cls, *args, **{**kwargs, "clock": lambda: after})

    monkeypatch.setattr(OwnershipRegistry, "_from_adopted_database", classmethod(open_after_expiry))


def test_dead_publisher_is_recovered_by_a_fresh_worker(delivery, monkeypatch):
    from capture_adoption import adopt_orphaned_capture_intents

    program = """
import os
import integration_adapter
from memory_queue import _QueueV3CandidateReader
from breadcrumb_capture import queue_breadcrumb
integration_adapter.spawn_detached = lambda *a, **k: None
_QueueV3CandidateReader.enqueue_capture_task_replay_safe = lambda *a, **k: os._exit(86)
queue_breadcrumb('user_prompt', 'demo', 'session-a', {'preview': 'survives process death'}, 'crash:one')
integration_adapter._run_active_capture_worker_once()
"""
    env = {**os.environ, "PYTHONPATH": str(integration_adapter.SCRIPTS_DIR)}
    exited = subprocess.run(
        [sys.executable, "-c", program],
        env=env,
        capture_output=True,
        # This waits for a real crash fixture, not the interactive hook's latency.
        timeout=LONG_TIMEOUT,
    )
    assert exited.returncode == 86, exited.stderr.decode()
    adoption = adopt_orphaned_capture_intents(delivery[2], delivery[3], state_root=delivery[1])
    assert adoption["skipped"][0]["reason"] == "OperationalOwnershipError: owner_busy"
    _expire_publisher_lease(delivery, monkeypatch)
    relative = _work(delivery)
    terminal = json.loads((delivery[1] / relative).read_bytes())
    assert terminal["disposition"]["kind"] == "markdown_committed"
    daily = next((delivery[0] / "knowledge/daily").glob("*.md"))
    assert daily.read_text().count("survives process death") == 1


def test_death_before_database_validation_keeps_the_breadcrumb(delivery):
    from capture_adoption import complete_pending_capture_intents

    program = """
import os
import markdown_transaction
import integration_adapter
from breadcrumb_capture import queue_breadcrumb
markdown_transaction.active_markdown_coordinator = lambda *a: os._exit(86)
integration_adapter._wake_capture_worker = lambda *a: os._exit(86)
queue_breadcrumb('user_prompt', 'demo', 'session-a', {'preview': 'retained before DB'}, 'crash:early')
"""
    env = {**os.environ, "PYTHONPATH": str(integration_adapter.SCRIPTS_DIR)}
    exited = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, timeout=LONG_TIMEOUT,
    )
    assert exited.returncode == 86, exited.stderr.decode()
    pending = list((delivery[1] / "run/capture-intents/pending").glob("*/*.json"))
    assert len(pending) == 1
    outcome = complete_pending_capture_intents(
        delivery[2], delivery[3], state_root=delivery[1],
        now=datetime.now().astimezone() + timedelta(hours=1),
    )
    assert len(outcome["completed"]) == 1
    assert outcome["skipped"] == []
    _work(delivery)
    daily = next((delivery[0] / "knowledge/daily").glob("*.md"))
    assert daily.read_text().count("retained before DB") == 1
    assert _work(delivery) is None


def test_retry_reuses_the_published_renderer_decision(delivery, monkeypatch):
    import breadcrumb_capture

    _publish_prompt()
    original = flush_memory._complete_capture_decision

    def interrupted(*args):
        raise RuntimeError("stopped after the decision was saved")

    monkeypatch.setattr(flush_memory, "_complete_capture_decision", interrupted)
    with pytest.raises(RuntimeError, match="decision was saved"):
        _work(delivery)
    with sqlite3.connect(delivery[2].db_path) as database:
        # Make the saved retry eligible without putting queue time ahead of the
        # independently clocked canonical owner and intent fences.
        database.execute("UPDATE tasks SET available_at=?", (datetime.now().astimezone().isoformat(),))
    monkeypatch.setattr(flush_memory, "_complete_capture_decision", original)
    monkeypatch.setattr(breadcrumb_capture, "_decision", lambda *a: pytest.fail("decision rebuilt"))
    terminal_path = _work(delivery)
    assert (
        json.loads((delivery[1] / terminal_path).read_bytes())["disposition"]["kind"]
        == "markdown_committed"
    )


def test_same_operation_cannot_change_its_payload(delivery):
    from breadcrumb_capture import queue_breadcrumb

    _publish_prompt()
    with pytest.raises(ValueError, match="different content"):
        queue_breadcrumb("user_prompt", "demo", "session-a", {"preview": "replacement"}, "one")
    record = json.loads(_pending_intents(delivery[1])[0].read_bytes())
    assert "original" in record["evidence"][0]["parts"][0]["text"]


def test_concurrent_breadcrumb_publishers_reuse_one_source(delivery, monkeypatch):
    import reliable_memory

    barrier = threading.Barrier(2)
    original = reliable_memory.publish_runtime_file

    def together(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(reliable_memory, "publish_runtime_file", together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _publish_prompt(), range(2)))
    monkeypatch.setattr(reliable_memory, "publish_runtime_file", original)
    assert results == [True, True]
    assert len(_pending_intents(delivery[1])) == 1
    _work(delivery)
    assert _work(delivery) is None
    daily = next((delivery[0] / "knowledge/daily").glob("*.md"))
    assert daily.read_text().count("original") == 1


def test_bad_unindexed_intent_does_not_hide_a_valid_breadcrumb(delivery):
    from capture_adoption import complete_pending_capture_intents

    assert _publish_prompt()
    bad = delivery[1] / ("run/capture-intents/pending/00/" + "0" * 64 + ".json")
    bad.parent.mkdir(mode=0o700, exist_ok=True)
    bad.write_bytes(b"{}")
    bad.chmod(0o600)
    outcome = complete_pending_capture_intents(
        delivery[2], delivery[3], state_root=delivery[1], limit=1,
    )
    assert len(outcome["completed"]) == 1
    assert len(outcome["skipped"]) == 1
    assert bad.exists()
    _work(delivery)
    assert "original" in next((delivery[0] / "knowledge/daily").glob("*.md")).read_text()


def test_unindexed_source_tampering_is_refused_before_queue_publication(delivery):
    from capture_adoption import complete_pending_capture_intents

    assert _publish_prompt()
    pending = _pending_intents(delivery[1])[0]
    pending.write_bytes(pending.read_bytes().replace(b"original", b"tampered"))
    outcome = complete_pending_capture_intents(delivery[2], delivery[3], state_root=delivery[1])
    assert outcome["completed"] == []
    assert len(outcome["skipped"]) == 1
    assert published_intents(delivery[1]) == []
    assert pending.exists()


def test_renderer_decision_cannot_change_source_bytes(delivery):
    from breadcrumb_capture import require_decision

    _publish_prompt()
    _work(delivery)
    state_root = delivery[1]
    intent = json.loads(published_intents(state_root)[0].read_bytes())
    decision = json.loads(
        next((state_root / "run/queue-results").glob("capture-decision-*.json")).read_bytes()
    )
    decision["operation_plan"][0]["block"] = "changed source"
    with pytest.raises(RuntimeError, match="does not match"):
        require_decision(decision, intent)


@pytest.mark.parametrize("legacy_id", ["one", "user-prompt:fallback:" + "a" * 64])
def test_completed_synchronous_capture_is_not_duplicated_after_upgrade(
    delivery, monkeypatch, legacy_id
):
    import breadcrumb_capture

    root, state_root, _, _ = delivery
    original = breadcrumb_capture.queue_breadcrumb
    monkeypatch.setattr(breadcrumb_capture, "queue_breadcrumb", lambda *args: False)
    assert user_prompt_capture._append_prompt_tag("demo", "session-a", "original", legacy_id)
    monkeypatch.setattr(breadcrumb_capture, "queue_breadcrumb", original)
    assert original(
        "user_prompt",
        "demo",
        "session-a",
        {"preview": "original"},
        legacy_id.replace(":fallback:", ":", 1),
    )
    assert _work(delivery) is None
    daily = next((root / "knowledge/daily").glob("*.md"))
    assert (published_intents(state_root), daily.read_text().count("original")) == ([], 1)
    assert _pending_intents(state_root) == []


@pytest.mark.shipped_append_budgets
@pytest.mark.parametrize("kind", ["prompt", "tool"])
def test_writer_contention_retains_the_event_for_a_worker(tmp_path, monkeypatch, kind):
    state_root, _ = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda *a, **k: None)
    errors = []
    monkeypatch.setattr(
        user_prompt_capture, "record_capture_failure", lambda *a, **k: errors.append(a)
    )
    monkeypatch.setattr(
        post_tool_capture, "record_capture_failure", lambda *a, **k: errors.append(a)
    )
    coordinator = active_markdown_coordinator(integration_adapter.ROOT, state_root)
    queue = active_memory_queue(integration_adapter.ROOT, state_root)
    calls = {
        "prompt": partial(
            user_prompt_capture._append_prompt_tag,
            "demo",
            "session-a",
            "Keep the original request",
            "prompt:one",
        ),
        "tool": partial(
            post_tool_capture._append_tool_tag,
            "demo",
            "session-a",
            "Edit",
            "src/original.py",
            "tool:one",
        ),
    }
    holder = active_markdown_coordinator(integration_adapter.ROOT, state_root)
    with holder.writer_gate():
        accepted = calls[kind]()
        retained = _pending_intents(state_root)
    assert (accepted, len(retained)) == (True, 1), errors
    worker = partial(flush_memory.process_new_capture, queue, coordinator)
    flush_memory.run_capture_worker_once(queue, coordinator, process_missing=worker)
    daily = list((integration_adapter.ROOT / "knowledge/daily").glob("*.md"))
    assert len(daily) == 1


def test_unindexed_ingress_is_backed_up_and_blocks_runtime_deletion(delivery, tmp_path):
    import time

    import installed_memory_repair as repair
    import private_vault_backup as backup

    from tests.slow_machine import SHORT_TIMEOUT

    root, state_root, queue, _ = delivery
    assert _publish_prompt()
    pending = _pending_intents(state_root)[0]
    original = pending.read_bytes()
    assert queue.capture_intent_record(pending.stem) is None
    assert "capture_intent_retained" in repair._queue_artifact_blockers(
        state_root, time.monotonic() + SHORT_TIMEOUT
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    with backup.staged_backup_image(
        root=root, state_root=state_root, staging_parent=staging,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        copied = image / "state" / pending.relative_to(state_root)
        assert copied.read_bytes() == original
    _work(delivery)
    assert "original" in next((root / "knowledge/daily").glob("*.md")).read_text()


def test_another_publisher_finishing_during_discovery_is_not_a_loss(delivery, monkeypatch):
    import capture_adoption as adoption

    _publish_prompt()
    _, state_root, queue, coordinator = delivery
    path = _pending_intents(state_root)[0]
    original = adoption._unindexed_pending_record
    record = original(path, state_root)

    def publish_before_read(pending, state):
        adoption._complete_one_pending(queue, coordinator, state, record)
        return original(pending, state)

    monkeypatch.setattr(adoption, "_unindexed_pending_record", publish_before_read)
    result = adoption.complete_pending_capture_intents(queue, coordinator, state_root=state_root)
    assert result["skipped"] == []
    _work(delivery)
    assert "original" in next((delivery[0] / "knowledge/daily").glob("*.md")).read_text()


def test_another_publisher_finishing_after_discovery_reuses_verified_ready(delivery, monkeypatch):
    import capture_adoption as adoption

    _publish_prompt()
    _, state_root, queue, coordinator = delivery
    pending = _pending_intents(state_root)[0]
    row = adoption._unindexed_pending_record(pending, state_root)
    payload = pending.read_bytes()
    original = adoption._verified_intent_bytes
    published = []

    def publish_before_read(state, record):
        if not published:
            published.append(True)
            integration_adapter._publish_capture_files_and_task(
                queue, coordinator, intent_id=row["intent_id"], payload=payload,
                intent_sha256=row["intent_sha256"], pending_relative=row["relative_path"],
                ready_relative=adoption._ready_relative_path(row),
            )
        return original(state, record)

    monkeypatch.setattr(adoption, "_verified_intent_bytes", publish_before_read)
    assert adoption._complete_one_pending(queue, coordinator, state_root, row) == row["intent_id"]
    _work(delivery)
    assert next((delivery[0] / "knowledge/daily").glob("*.md")).read_text().count("original") == 1


@pytest.mark.parametrize("ready_bytes", [None, b"changed"])
def test_missing_pending_requires_the_exact_ready_bytes(delivery, ready_bytes):
    import capture_adoption as adoption

    _publish_prompt()
    _, state_root, queue, coordinator = delivery
    pending = _pending_intents(state_root)[0]
    row = adoption._unindexed_pending_record(pending, state_root)
    pending.unlink()
    if ready_bytes is not None:
        ready = state_root / adoption._ready_relative_path(row)
        ready.write_bytes(ready_bytes)
        ready.chmod(0o600)
    with pytest.raises((FileNotFoundError, ValueError)):
        adoption._complete_one_pending(queue, coordinator, state_root, row)
    assert queue.capture_intent_record(row["intent_id"]) is None
