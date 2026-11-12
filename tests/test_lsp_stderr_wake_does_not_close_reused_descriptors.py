"""A repeated cleanup must never close a descriptor now owned by another file."""
import errno
import os
import threading
import time

import pytest
from llm_wiki import lsp_process_cleanup, lsp_process_state

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX stderr wake owns native pipe descriptors')


def _close_after_fault(stream):
    try:
        stream.close()
    except OSError as error:
        if error.errno != errno.EBADF:
            raise


def test_a_second_unstarted_drain_cleanup_does_not_close_an_unrelated_file(tmp_path):
    path = tmp_path / 'unrelated.txt'
    path.write_bytes(b'kept by its own reader')
    wake = lsp_process_state.new_stderr_wake()
    generation = lsp_process_state.Generation('test-generation', None, None)
    generation.stderr_wake = wake
    generation.stderr_thread = threading.Thread(target=lambda: None)
    original = wake.read_fd
    assert lsp_process_cleanup.stop_stderr_drain(generation, time.monotonic()) is True
    borrowed = path.open('rb')
    try:
        assert borrowed.fileno() == original
        assert lsp_process_cleanup.stop_stderr_drain(generation, time.monotonic()) is True
        assert borrowed.read() == b'kept by its own reader'
    finally:
        _close_after_fault(borrowed)


def test_an_uncertain_close_is_reported_and_never_retried_against_a_reused_fd(tmp_path, monkeypatch):
    path = tmp_path / 'unrelated.txt'
    path.write_bytes(b'kept')
    wake = lsp_process_state.new_stderr_wake()
    original = wake.read_fd
    native_close = os.close

    def uncertain_close(descriptor):
        native_close(descriptor)
        if descriptor == original:
            raise OSError(errno.EINTR, 'injected uncertain native close')

    monkeypatch.setattr(os, 'close', uncertain_close)
    with pytest.raises(OSError, match='uncertain native close'):
        wake.abandon()
    monkeypatch.setattr(os, 'close', native_close)
    borrowed = path.open('rb')
    try:
        assert borrowed.fileno() == original
        with pytest.raises(OSError, match='uncertain native close'):
            wake.abandon()
        assert borrowed.read() == b'kept'
    finally:
        _close_after_fault(borrowed)


def test_joined_drain_reports_a_close_error_recorded_by_its_owner(tmp_path, monkeypatch):
    from collections import deque

    from tests.slow_machine import LONG_TIMEOUT

    wake = lsp_process_state.new_stderr_wake()
    original = wake.read_fd
    native_close = os.close
    read_fd, write_fd = os.pipe()
    native_close(write_fd)
    stream = os.fdopen(read_fd, 'rb')
    generation = lsp_process_state.Generation('test-generation', None, None)
    generation.stderr_wake = wake
    thread = threading.Thread(target=lsp_process_state.drain_stderr,
        args=(stream, deque(), [0], threading.Lock(), wake))
    generation.stderr_thread = thread

    def uncertain_close(descriptor):
        native_close(descriptor)
        if descriptor == original:
            raise OSError(errno.EINTR, 'injected drain close uncertainty')

    monkeypatch.setattr(os, 'close', uncertain_close)
    thread.start()
    thread.join(LONG_TIMEOUT)
    assert not thread.is_alive()
    monkeypatch.setattr(os, 'close', native_close)
    with pytest.raises(OSError, match='drain close uncertainty'):
        lsp_process_cleanup.stop_stderr_drain(generation, time.monotonic())
    assert wake.read_fd is None
    assert wake.write_fd is None
