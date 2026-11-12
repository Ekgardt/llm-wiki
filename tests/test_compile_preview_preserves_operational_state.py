"""Preview failures must not rewrite the outcome of real compilation."""
from types import SimpleNamespace

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_claims

from tests.compile_paths import point_compile_path


def _forbidden(*args, **kwargs):
    pytest.fail('preview attempted operational maintenance')


def test_empty_preview_does_not_repair_or_retire_source_state(monkeypatch):
    point_compile_path(monkeypatch, 'load_state', lambda: {})
    point_compile_path(monkeypatch, 'active_or_legacy_coordinator', lambda *a, **k: SimpleNamespace(state_root=None))
    point_compile_path(monkeypatch, 'receipt_predicate', lambda *a, **k: None)
    point_compile_path(monkeypatch, 'select_dailies', lambda *a, **k: [])
    point_compile_path(monkeypatch, 'repair_compile_mirror', _forbidden)
    point_compile_path(monkeypatch, 'retire_stale_source_failures', _forbidden)
    point_compile_path(monkeypatch, 'mark_finished', _forbidden)
    assert compile_pipeline.run(SimpleNamespace(dry_run=True, trigger='manual')) == 0


def test_failed_preview_does_not_replace_last_real_completion(monkeypatch):
    point_compile_path(monkeypatch, 'mark_finished', _forbidden)
    point_compile_path(monkeypatch, 'load_state', lambda: {'last_compile_error': 'real failure'})
    assert compile_pipeline.finish_run(SimpleNamespace(dry_run=True, trigger='manual'),
                                [compile_pipeline_claims.BatchOutcome(1)]) == 1


def test_preview_resolution_failure_does_not_write_source_failure(monkeypatch):
    point_compile_path(monkeypatch, 'require_ready_compile_batch', lambda *a: None)
    point_compile_path(monkeypatch, '_resolve_batch_work', _resolution_error)
    point_compile_path(monkeypatch, 'record_compile_source_failures', _forbidden)
    point_compile_path(monkeypatch, '_record_batch_error', _forbidden)
    result = compile_pipeline.run_batch(SimpleNamespace(inputs=None), SimpleNamespace(dry_run=True),
        coordinator=None, deadline=float('inf'), cancelled=None, owner=None)
    assert result.status == 1


def _resolution_error(*args):
    raise ValueError('invalid preview proposal')


def test_preview_refused_source_does_not_record_failure(monkeypatch):
    point_compile_path(monkeypatch, 'record_compile_source_failures', _forbidden)
    point_compile_path(monkeypatch, '_record_batch_error', _forbidden)
    day = SimpleNamespace(dailies=[SimpleNamespace(logical_path='knowledge/daily/2026-10-20.md')])
    assert compile_pipeline.refused_unit_outcome(day, record=False).status == 1


def test_preview_repair_child_failure_does_not_record_failure(monkeypatch):
    point_compile_path(monkeypatch, 'refresh_compile_batch', _refresh_error)
    point_compile_path(monkeypatch, 'record_compile_source_failures', _forbidden)
    point_compile_path(monkeypatch, '_record_batch_error', _forbidden)
    outcomes = compile_pipeline.run_repair_child(SimpleNamespace(inputs=None), (),
        SimpleNamespace(dry_run=True), {'deadline': float('inf'), 'cancelled': None})
    assert [outcome.status for outcome in outcomes] == [1]


def _refresh_error(*args, **kwargs):
    raise ValueError('preview source changed')


def test_real_failure_still_records_source_and_run_failure(monkeypatch):
    recorded = []
    point_compile_path(monkeypatch, 'record_compile_source_failures', lambda *a, **k: recorded.append('source'))
    point_compile_path(monkeypatch, '_record_batch_error', lambda *a, **k: recorded.append('run'))
    assert compile_pipeline.record_failed_batch(None, ValueError('real failure')) == 1
    assert recorded == ['source', 'run']
