"""Exhausting an idle wait does not authorize changing another compiler's inputs."""
import pytest
import scheduled_nightly as nightly


def _prepare(monkeypatch):
    seen, messages, remembered = [], [], []
    monkeypatch.setattr(nightly, '_intake_steps', lambda: [])
    monkeypatch.setattr(nightly, '_wait_for_compile_idle', lambda log: None)
    monkeypatch.setattr(nightly, '_last_compile_finished', lambda: None)
    monkeypatch.setattr(nightly, '_last_compile_started', lambda: 'existing-start')
    monkeypatch.setattr(nightly, '_wait_compile_finished', lambda: True)
    monkeypatch.setattr(nightly, '_report_compile_outcome', lambda *args: 0)
    monkeypatch.setattr(nightly, '_report_deferred_loss', lambda log: 0)
    monkeypatch.setattr(nightly, '_post_compile_pass', lambda *args: 0)
    monkeypatch.setattr(nightly, '_remember_deferred_compile', lambda log: remembered.append(True))
    return seen, messages, remembered


def _runner(path, seen):
    def run(command, log, name, *, timeout):
        seen.append(name)
        if name == 'fact_keys':
            path.write_bytes(b'changed entity page\n')
        return int(name == 'intake')
    return run


def test_exhausted_idle_wait_does_not_change_a_running_compilers_target(tmp_path, monkeypatch):
    seen, messages, remembered = _prepare(monkeypatch)
    path = tmp_path / 'entity.md'
    path.write_bytes(b'original compiler target\n')
    monkeypatch.setattr(nightly.maybe_compile, 'status', lambda: {'compile_running': True})
    result = nightly._nightly_steps(_runner(path, seen), nightly.StepLog(messages.append))
    assert result == 0
    assert seen == []
    assert path.read_bytes() == b'original compiler target\n'
    assert remembered == [True]
    assert 'outcome is unknown' in '\n'.join(messages)


def test_a_compiler_that_finished_during_the_wait_allows_the_original_order(tmp_path, monkeypatch):
    seen, messages, remembered = _prepare(monkeypatch)
    path = tmp_path / 'entity.md'
    path.write_bytes(b'original compiler target\n')
    monkeypatch.setattr(nightly.maybe_compile, 'status', lambda: {'compile_running': False})
    result = nightly._nightly_steps(_runner(path, seen), nightly.StepLog(messages.append))
    assert result == 0
    assert seen == ['fact_keys', 'maybe_compile']
    assert path.read_bytes() == b'changed entity page\n'
    assert remembered == []


def test_unreadable_compile_status_cannot_authorize_entity_writes(tmp_path, monkeypatch):
    seen, messages, remembered = _prepare(monkeypatch)
    path = tmp_path / 'entity.md'
    path.write_bytes(b'original compiler target\n')

    def unreadable():
        raise OSError('controlled compile status failure')

    monkeypatch.setattr(nightly.maybe_compile, 'status', unreadable)
    with pytest.raises(OSError, match='controlled compile status failure'):
        nightly._nightly_steps(_runner(path, seen), nightly.StepLog(messages.append))
    assert seen == []
    assert path.read_bytes() == b'original compiler target\n'
    assert remembered == []


def test_deferring_entity_writes_preserves_an_existing_intake_failure(tmp_path, monkeypatch):
    seen, messages, remembered = _prepare(monkeypatch)
    path = tmp_path / 'entity.md'
    path.write_bytes(b'original compiler target\n')
    step = nightly._Step(message='controlled intake', command=['intake'], label='intake', timeout=1)
    monkeypatch.setattr(nightly, '_intake_steps', lambda: [step])
    monkeypatch.setattr(nightly.maybe_compile, 'status', lambda: {'compile_running': True})
    result = nightly._nightly_steps(_runner(path, seen), nightly.StepLog(messages.append))
    assert result == 1
    assert seen == ['intake']
    assert path.read_bytes() == b'original compiler target\n'
    assert remembered == [True]
