"""Preview failures must not rewrite the outcome of real compilation."""
from types import SimpleNamespace

import compile_memory as compiler
import pytest


def _forbidden(*args, **kwargs):
    pytest.fail('preview attempted operational maintenance')


def test_empty_preview_does_not_repair_or_retire_source_state(monkeypatch):
    monkeypatch.setattr(compiler, 'load_state', lambda: {})
    monkeypatch.setattr(compiler, 'active_or_legacy_coordinator',
                        lambda *a, **k: SimpleNamespace(state_root=None))
    monkeypatch.setattr(compiler, '_receipt_predicate', lambda *a, **k: None)
    monkeypatch.setattr(compiler, 'select_dailies', lambda *a, **k: [])
    monkeypatch.setattr(compiler, '_repair_compile_mirror', _forbidden)
    monkeypatch.setattr(compiler, '_retire_stale_source_failures', _forbidden)
    monkeypatch.setattr(compiler, '_mark_finished', _forbidden)
    assert compiler._run(SimpleNamespace(dry_run=True, trigger='manual')) == 0


def test_failed_preview_does_not_replace_last_real_completion(monkeypatch):
    monkeypatch.setattr(compiler, '_mark_finished', _forbidden)
    monkeypatch.setattr(compiler, 'load_state', lambda: {'last_compile_error': 'real failure'})
    assert compiler._finish_run(SimpleNamespace(dry_run=True, trigger='manual'),
                                [compiler.BatchOutcome(1)]) == 1


def test_preview_resolution_failure_does_not_write_source_failure(monkeypatch):
    monkeypatch.setattr(compiler, '_require_ready_compile_batch', lambda *a: None)
    monkeypatch.setattr(compiler, '_resolve_batch_work', _resolution_error)
    monkeypatch.setattr(compiler, '_record_compile_source_failures', _forbidden)
    monkeypatch.setattr(compiler, '_record_batch_error', _forbidden)
    result = compiler._run_batch(SimpleNamespace(inputs=None), SimpleNamespace(dry_run=True),
        coordinator=None, deadline=float('inf'), cancelled=None, owner=None)
    assert result.status == 1


def _resolution_error(*args):
    raise ValueError('invalid preview proposal')


def test_preview_refused_source_does_not_record_failure(monkeypatch):
    monkeypatch.setattr(compiler, '_record_compile_source_failures', _forbidden)
    monkeypatch.setattr(compiler, '_record_batch_error', _forbidden)
    day = SimpleNamespace(dailies=[SimpleNamespace(logical_path='knowledge/daily/2026-10-20.md')])
    assert compiler._refused_unit_outcome(day, record=False).status == 1


def test_preview_repair_child_failure_does_not_record_failure(monkeypatch):
    monkeypatch.setattr(compiler, '_refresh_compile_batch', _refresh_error)
    monkeypatch.setattr(compiler, '_record_compile_source_failures', _forbidden)
    monkeypatch.setattr(compiler, '_record_batch_error', _forbidden)
    outcomes = compiler._run_repair_child(SimpleNamespace(inputs=None), (),
        SimpleNamespace(dry_run=True), {'deadline': float('inf'), 'cancelled': None})
    assert [outcome.status for outcome in outcomes] == [1]


def _refresh_error(*args, **kwargs):
    raise ValueError('preview source changed')


def test_real_failure_still_records_source_and_run_failure(monkeypatch):
    recorded = []
    monkeypatch.setattr(compiler, '_record_compile_source_failures',
                        lambda *a, **k: recorded.append('source'))
    monkeypatch.setattr(compiler, '_record_batch_error',
                        lambda *a, **k: recorded.append('run'))
    assert compiler._record_failed_batch(None, ValueError('real failure')) == 1
    assert recorded == ['source', 'run']
