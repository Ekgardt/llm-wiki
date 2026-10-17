"""Storage errors never become accepted capture, including an existing-file retry."""
from __future__ import annotations

import errno
import os
from functools import partial

import breadcrumb_protocol
import pytest
import reliable_memory

from tests.test_breadcrumb_storage import SCOPE, _bundle, _publish
from tests.test_queue_v3_capture_links import _coordinator, _queue

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file/directory fsync path")


def _staged_file(path, directory, suffix):
    return path.parent == directory and f"{suffix}." in path.name


def _published_directory(path, directory, suffix):
    return path == directory and any(item.name.endswith(suffix) for item in directory.iterdir())


def _install_fault(monkeypatch, directory, suffix, fault):
    staged = partial(_staged_file, directory=directory, suffix=suffix)
    published = partial(_published_directory, directory=directory, suffix=suffix)
    choices = {
        "write": ("_write_staged_bytes", staged, errno.ENOSPC),
        "file-sync": ("fsync_file", staged, errno.EIO),
        "directory-sync": ("fsync_directory", published, errno.EIO),
    }
    name, selected, error_number = choices[fault]
    original = getattr(reliable_memory, name)

    def refused(path, *args, **kwargs):
        if selected(path):
            raise OSError(error_number, f"injected {fault} failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(reliable_memory, name, refused)


@pytest.mark.parametrize("suffix", [".anchor", ".part", ".json"])
@pytest.mark.parametrize("fault", ["write", "file-sync", "directory-sync"])
def test_storage_fault_refuses_acceptance_and_replay_recovers(tmp_path, monkeypatch, suffix, fault):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    identity = breadcrumb_protocol.occurrence_identity(SCOPE)
    directory = tmp_path / "run/capture-intents/pending" / identity[:2]

    with monkeypatch.context() as failures:
        _install_fault(failures, directory, suffix, fault)
        for _attempt in range(2):  # First publication and a retry with surviving files.
            with pytest.raises(OSError, match=f"injected {fault} failure"):
                _publish(queue, coordinator)
            assert queue.claim_capture("must-not-accept", handler_versions=(2,)) is None

    published = _publish(queue, coordinator)
    assert published.registered
    assert _bundle(tmp_path, identity).content == reliable_memory.canonical_json_bytes(
        {"prompt": "complete prompt"}
    )
    assert queue.claim_capture("after-storage-recovery", handler_versions=(2,)) is not None
    assert queue.claim_capture("no-duplicate", handler_versions=(2,)) is None
