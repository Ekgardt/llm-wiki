"""A directory that cannot be read must not look like an empty outbox."""
from __future__ import annotations

import os

import breadcrumb_storage as storage
import pytest

from tests.test_breadcrumb_adoption import _orphan


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX directory permission bits")
@pytest.mark.parametrize("level", ["pending", "shard"])
def test_inaccessible_pending_directory_is_reported(tmp_path, monkeypatch, level):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    directory = storage.anchor_path(tmp_path, identity).parent
    if level == "pending":
        directory = directory.parent
    directory.chmod(0)
    try:
        result = storage.recover_pending(queue, coordinator)
    finally:
        directory.chmod(0o700)
    assert result["skipped"]
    assert "PermissionError" in result["skipped"][0]["reason"]
    assert storage._stored_manifest(tmp_path, identity)


def test_symlinked_pending_directory_is_refused(tmp_path, monkeypatch):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    shard = storage.anchor_path(tmp_path, identity).parent
    relocated = tmp_path / "relocated"
    shard.rename(relocated)
    shard.symlink_to(relocated, target_is_directory=True)
    result = storage.recover_pending(queue, coordinator)
    assert result["skipped"]
    assert result["recovered"] == []
    assert queue.claim_capture("test", handler_versions=(2,)) is None
