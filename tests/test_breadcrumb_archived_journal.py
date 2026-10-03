"""A real BagIt archive remains proof of an already committed journal append."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

import archive_daily
import compile_memory
import flush_memory
import markdown_transaction
import memory_queue
import pytest

from tests.test_breadcrumb_terminal_proof import _committed, _complete, _delivery_pair
from tests.test_capture_terminal import _expire_capture_lease
from tests.test_queue_v3_capture_links import _adopted_vault


def _compile_configuration(root, state, monkeypatch):
    locations = {
        "ROOT": root, "STATE_ROOT": state, "MEMORY": root / "knowledge",
        "DAILY_DIR": root / "knowledge/daily", "KNOWLEDGE": root / "knowledge/notes",
        "INDEX": root / "knowledge/index.md", "LOG": root / "knowledge/log.local.md",
        "AGENTS": root / "AGENTS.md",
    }
    for name, path in locations.items():
        monkeypatch.setattr(compile_memory, name, path)
    (root / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text("test archive contract\n")
    (root / "knowledge/index.md").write_text("# Index\n")
    (root / "knowledge/log.local.md").write_text("# Log\n")


def _compile_for_archive(coordinator, daily):
    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    compile_memory.apply_compile_plan(
        inputs, {"schema_version": "compile-plan/v2", "operations": []},
        action_key="d" * 64, trigger="manual", coordinator=coordinator,
        completed_at="2026-09-29T00:00:00Z", batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )


def _archived_delivery(tmp_path, monkeypatch, *, complete=True):
    root, state = _adopted_vault(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    coordinator = markdown_transaction.active_markdown_coordinator(root, state)
    with _delivery_pair(queue, coordinator) as arguments:
        transaction = _committed(arguments)
        if complete:
            _complete(arguments, flush_memory._capture_markdown_disposition(transaction, arguments[7]))
    _compile_configuration(root, state, monkeypatch)
    daily = root / arguments[-1]["operation_plan"][0]["path"]
    _compile_for_archive(coordinator, daily)
    archiver = archive_daily.DailyArchiver(
        root, state, queue=queue, clock=lambda: datetime(2027, 2, 1, tzinfo=timezone.utc),
    )
    receipt = archiver.archive(daily.stem)
    assert receipt.state == "archived"
    assert not daily.exists()
    return arguments, receipt


def _proof(arguments):
    queue, _coordinator, lease = arguments[:3]
    with queue.connection() as database:
        binding = queue.active_capture_binding(database, lease.id)
        return queue._require_capture_terminal_proof(database, lease.id, binding)


def test_sealed_bag_is_accepted_after_the_flat_journal_is_archived(tmp_path, monkeypatch):
    arguments, _receipt = _archived_delivery(tmp_path, monkeypatch)

    assert _proof(arguments)["disposition"]["kind"] == "markdown_committed"


def test_present_conflicting_journal_does_not_fall_back_to_archive(tmp_path, monkeypatch):
    arguments, _receipt = _archived_delivery(tmp_path, monkeypatch)
    daily = arguments[1].vault / arguments[-1]["operation_plan"][0]["path"]
    daily.write_bytes(b"changed present journal")

    with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb"):
        _proof(arguments)


def test_delayed_worker_does_not_recreate_a_journal_already_in_an_archive(tmp_path, monkeypatch):
    arguments, _receipt = _archived_delivery(tmp_path, monkeypatch, complete=False)
    queue, coordinator, lease = arguments[:3]
    _expire_capture_lease(queue, lease.id)

    result = flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )

    assert result
    assert not (coordinator.vault / arguments[-1]["operation_plan"][0]["path"]).exists()
    assert _proof(arguments)["disposition"]["kind"] == "markdown_committed"
