"""A fresh process recovers full accepted events without the producer's memory."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import breadcrumb_storage as storage
import memory_queue
import pytest
from reliable_memory import canonical_json_bytes

from tests.slow_machine import LONG_TIMEOUT
from tests.test_breadcrumb_storage import _bundle, _publish, _registration_failure
from tests.test_queue_v3_capture_links import _coordinator, _queue

_CRASH_CODE = """
import os
import sys
from pathlib import Path
from tests.test_queue_v3_capture_links import _queue, _coordinator
from tests.test_breadcrumb_storage import _publish
import integration_adapter
root = Path(sys.argv[1])
queue, coordinator = _queue(root), _coordinator(root)
targets = {
    'before_index': (queue, 'index_capture_intent_pending'),
    'before_enqueue': (queue, 'enqueue_capture_task_replay_safe'),
    'after_enqueue': (integration_adapter, '_remove_verified_pending'),
}
target, name = targets[sys.argv[2]]
setattr(target, name, lambda *args, **kwargs: os._exit(73))
_publish(queue, coordinator, {'prompt': 'full evidence ' * 100000})
raise AssertionError('crash boundary was not reached')
"""


def _crash_publisher(root: Path, boundary: str):
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_CODE, str(root), boundary],
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 73, result.stderr
    return memory_queue.MemoryQueue._from_v3_candidate(
        root / "run/queue-v3.candidate.sqlite3", state_root=root,
    )


def _wait_for_persisted_lease_expiry(root: Path) -> None:
    """Wait for the real lease deadline; never replace the OS death proof."""
    with closing(sqlite3.connect(root / "run/markdown-transactions-v3.candidate.sqlite3")) as database:
        row = database.execute("SELECT MAX(expires_at) FROM maintenance_owners WHERE role='capture'").fetchone()
    expiry = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    threading.Event().wait(max(0, remaining))


@pytest.mark.parametrize("boundary", ["before_index", "before_enqueue", "after_enqueue"])
def test_crashed_publisher_is_recovered_from_disk_without_its_identity(tmp_path, boundary):
    queue = _crash_publisher(tmp_path, boundary)
    _wait_for_persisted_lease_expiry(tmp_path)

    result = storage.recover_pending(queue, _coordinator(tmp_path))

    assert result["skipped"] == []
    assert len(result["recovered"]) == 1
    lease = queue.claim_capture("restarted", handler_versions=(2,))
    assert lease is not None
    bundle = _bundle(tmp_path, lease.payload["intent_id"])
    assert bundle.content == canonical_json_bytes({"prompt": "full evidence " * 100000})
    assert queue.claim_capture("restarted", handler_versions=(2,)) is None
    assert storage.recover_pending(queue, _coordinator(tmp_path))["recovered"] == []


def test_damaged_pending_input_is_kept_and_does_not_hide_another_event(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    index = queue.index_capture_intent_pending
    monkeypatch.setattr(queue, "index_capture_intent_pending", _registration_failure)
    damaged = _publish(queue, coordinator)
    intact = _publish(queue, coordinator, scope={"occurrence": "another"})
    bundle = _bundle(tmp_path, damaged.intent_id)
    storage.part_path(tmp_path, damaged.intent_id, storage.protocol.read_manifest(bundle.manifest)["last_part_sha256"]).unlink()
    monkeypatch.setattr(queue, "index_capture_intent_pending", index)

    result = storage.recover_pending(queue, coordinator)

    assert result["recovered"] == [intact.intent_id]
    assert [item["intent_id"] for item in result["skipped"]] == [damaged.intent_id]
    assert storage.anchor_path(tmp_path, damaged.intent_id).with_suffix(".json").is_file()
    assert queue.claim_capture("test", handler_versions=(2,)).payload["intent_id"] == intact.intent_id


@pytest.mark.parametrize("version", ["capture-intent/v1", "capture-intent/v99"])
def test_incomplete_or_unknown_formats_are_reported_without_dispatch(tmp_path, version):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    path = tmp_path / "run/capture-intents/pending/aa" / ("a" * 64 + ".json")
    path.parent.mkdir(parents=True)
    content = canonical_json_bytes({"schema_version": version})
    storage._publish(tmp_path, path, content)

    result = storage.recover_pending(queue, coordinator)

    assert result["skipped"]
    assert result["recovered"] == []
    assert path.read_bytes() == content
    assert queue.claim_capture("test", handler_versions=(1, 2)) is None
