"""A pre-existing result is compared through the same safe reader as recovery."""
from pathlib import Path

import memory_queue
import pytest


def test_existing_symlink_is_not_accepted_as_durable_result(tmp_path):
    target = tmp_path / 'outside-result'
    target.write_bytes(b'answer')
    memory_queue._harden_owner_only(target, 0o600)
    result = tmp_path / 'result'
    result.symlink_to(target)
    with pytest.raises(memory_queue.QueueOperationError, match='durable_file_conflict'):
        memory_queue._write_durable_file(result, b'answer')
    assert target.read_bytes() == b'answer'


def test_existing_file_does_not_use_an_unbounded_path_read(tmp_path, monkeypatch):
    result = tmp_path / 'result'
    memory_queue._write_durable_file(result, b'answer')
    monkeypatch.setattr(Path, 'read_bytes', _unbounded_read_forbidden)
    memory_queue._write_durable_file(result, b'answer')


def _unbounded_read_forbidden(*args):
    raise AssertionError('unbounded Path.read_bytes in conflict handling')


def test_different_existing_bytes_remain_untouched(tmp_path):
    result = tmp_path / 'result'
    memory_queue._write_durable_file(result, b'original answer')
    with pytest.raises(memory_queue.QueueOperationError, match='durable_file_conflict'):
        memory_queue._write_durable_file(result, b'other')
    assert result.read_bytes() == b'original answer'
