"""A lost file snapshot retries the whole append without dropping the winner."""
import time

import markdown_transaction as transaction
import pytest

from tests.slow_machine import LONG_TIMEOUT

RELATIVE = 'knowledge/daily/capture-race.md'


def _coordinator(tmp_path):
    vault = tmp_path / 'vault'
    (vault / 'knowledge/daily').mkdir(parents=True)
    return transaction.MarkdownCoordinator(vault, tmp_path / 'state')


def _append(coordinator):
    return transaction._append_until_committed(
        coordinator, 'capture-race', RELATIVE, b'our event\n',
        deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None,
    )


@pytest.mark.parametrize('phase', ['_staged_states', '_read_stable_descriptor'])
def test_changed_file_during_capture_retries_without_losing_either_append(tmp_path, monkeypatch, phase):
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / RELATIVE
    target.write_bytes(b'base\n')
    original = getattr(coordinator, phase)
    changed = False

    def change_once():
        nonlocal changed
        if not changed:
            changed = True
            target.write_bytes(b'base\nother event\n')

    def concurrent_write(*args):
        if phase == '_read_stable_descriptor':
            result = original(*args)
            change_once()
            return result
        change_once()
        return original(*args)

    monkeypatch.setattr(coordinator, phase, concurrent_write)
    first = _append(coordinator)
    second = _append(coordinator)

    assert changed
    assert first.state == second.state == 'committed'
    assert first.id == second.id
    assert target.read_bytes() == b'base\nother event\nour event\n'


@pytest.mark.parametrize('method', ['_require_unchanged_read', '_require_unchanged_hash'])
@pytest.mark.parametrize('replacement', [False, True])
def test_changed_snapshot_is_a_typed_precondition_loss(tmp_path, method, replacement):
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / RELATIVE
    target.write_bytes(b'before')
    before = target.stat()
    changed = target.with_suffix('.replacement') if replacement else target
    changed.write_bytes(b'after with different length')
    if replacement:
        changed.replace(target)

    with pytest.raises(transaction.PreconditionChangedError, match='target changed while'):
        getattr(coordinator, method)(before, target.stat(), target)


def test_invalid_staging_input_is_not_retried(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / RELATIVE
    target.write_bytes(b'base\n')
    calls = []

    def invalid_input(*args):
        calls.append(args)
        raise ValueError('invalid staged input')

    monkeypatch.setattr(coordinator, '_staged_states', invalid_input)
    with pytest.raises(ValueError, match='invalid staged input'):
        _append(coordinator)
    assert len(calls) == 1
    assert target.read_bytes() == b'base\n'


def test_capture_retry_still_obeys_cancellation(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / RELATIVE
    target.write_bytes(b'base\n')
    original = coordinator._read_stable_descriptor
    cancelled = False

    def changed_capture(*args):
        nonlocal cancelled
        result = original(*args)
        target.write_bytes(b'base\nother event\n')
        cancelled = True
        return result

    monkeypatch.setattr(coordinator, '_read_stable_descriptor', changed_capture)
    with pytest.raises(TimeoutError, match='cancellation'):
        transaction._append_until_committed(
            coordinator, 'cancelled-capture', RELATIVE, b'our event\n',
            deadline=time.monotonic() + LONG_TIMEOUT, cancelled=lambda: cancelled,
        )
    assert target.read_bytes() == b'base\nother event\n'
