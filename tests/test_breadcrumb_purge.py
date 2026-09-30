"""Cleanup exports every runtime part and leaves the complete permanent source."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import breadcrumb_evidence
import flush_memory
import memory_queue
import pytest
from reliable_memory import sha256_bytes

from tests.test_breadcrumb_terminal_proof import _committed, _complete, _delivery

CUTOFF = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _completed(root):
    with _delivery(root) as arguments:
        transaction = _committed(arguments)
        _complete(arguments, flush_memory._capture_markdown_disposition(transaction, arguments[7]))
        queue, _coordinator, lease = arguments[:3]
        with sqlite3.connect(queue.db_path) as database:
            database.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (lease.id,))
        return arguments


def _runtime_sources(root):
    directory = root / "run/capture-intents"
    return {str(path.relative_to(root)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def _assert_export_preserves_sources(root, export, before):
    manifest = json.loads((export / "manifest.json").read_bytes())
    archived = {item["source_path"]: item for item in manifest["capture_artifacts"]}
    assert set(before) <= set(archived)
    for path, data in before.items():
        assert (export / archived[path]["archive_path"]).read_bytes() == data
        assert archived[path]["sha256"] == sha256_bytes(data)
        assert not (root / path).exists()


def test_purge_exports_and_removes_every_runtime_source(tmp_path):
    arguments = _completed(tmp_path)
    before = _runtime_sources(tmp_path)
    export = tmp_path / "exports/complete"

    receipt = arguments[0].purge(terminal_before=CUTOFF, export_path=export)

    assert receipt.purged == 1
    _assert_export_preserves_sources(tmp_path, export, before)
    assert breadcrumb_evidence.read_permanent_source(tmp_path, arguments[-1]["source"]["path"]) == arguments[6].content


def test_cleanup_crash_resumes_from_export_without_runtime_manifest(tmp_path, monkeypatch):
    arguments = _completed(tmp_path)
    queue = arguments[0]
    before = _runtime_sources(tmp_path)
    export = tmp_path / "exports/retry"
    unlink = queue._unlink_capture_purge_artifact

    def fail_after_manifest(export_path, task_id, artifact):
        unlink(export_path, task_id, artifact)
        if artifact.source_path.endswith(".json"):
            raise OSError("cleanup process interrupted")

    monkeypatch.setattr(queue, "_unlink_capture_purge_artifact", fail_after_manifest)
    with pytest.raises(OSError, match="interrupted"):
        queue.purge(terminal_before=CUTOFF, export_path=export)
    monkeypatch.setattr(queue, "_unlink_capture_purge_artifact", unlink)

    queue.purge(terminal_before=CUTOFF, export_path=export)

    _assert_export_preserves_sources(tmp_path, export, before)


def test_source_damage_after_completion_prevents_purge(tmp_path):
    arguments = _completed(tmp_path)
    before = _runtime_sources(tmp_path)
    (tmp_path / arguments[-1]["source"]["path"]).unlink()

    with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb_terminal_unverified"):
        arguments[0].purge(terminal_before=CUTOFF, export_path=tmp_path / "exports/damaged")

    assert _runtime_sources(tmp_path) == before
