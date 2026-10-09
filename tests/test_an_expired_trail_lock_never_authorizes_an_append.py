"""An unavailable sidecar is a refused append, never permission to lose a line."""
import threading

import capture_diagnostics as diagnostics
import pytest

from tests import test_a_trim_never_drops_a_line_it_did_not_read as trail_tests
from tests.slow_machine import SHORT_TIMEOUT


@pytest.fixture(name='trail')
def configured_trail(tmp_path, monkeypatch):
    return trail_tests.trail.__wrapped__(tmp_path, monkeypatch)


def test_an_expired_lock_does_not_report_a_write_that_a_trim_can_erase(trail):
    trail.write_text('{"reason":"old"}\n')
    results = []
    with diagnostics.trail_lock() as held:
        original = trail.read_bytes()
        worker = threading.Thread(target=lambda: results.append(
            diagnostics._append_failure_line({'reason': 'new'})))
        worker.start()
        worker.join(SHORT_TIMEOUT)
        assert held and not worker.is_alive()
        before_replace = trail.read_bytes()
        diagnostics.atomic_write(trail, original.decode())
    assert (results, before_replace, trail.read_bytes()) == ([False], original, original)


def test_a_failed_lock_open_never_authorizes_a_write(trail, monkeypatch):
    trail.write_bytes(b'original\n')
    monkeypatch.setattr(diagnostics, '_open_trail_lock', lambda: None)
    assert diagnostics._append_failure_line({'reason': 'new'}) is False
    assert trail.read_bytes() == b'original\n'


def test_reentrancy_does_not_authorize_a_different_trail(trail, monkeypatch):
    other = trail.with_name('other.jsonl')
    other.write_bytes(b'original\n')
    with diagnostics.trail_lock() as held:
        assert held
        monkeypatch.setattr(diagnostics, 'FAILURE_LOG', other)
        monkeypatch.setattr(diagnostics, '_open_trail_lock', lambda: None)
        assert diagnostics._append_failure_line({'reason': 'new'}) is False
    assert other.read_bytes() == b'original\n'
