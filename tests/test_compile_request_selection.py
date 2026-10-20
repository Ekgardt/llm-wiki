"""Queued compilation must retain the caller's source-selection intent."""
from types import SimpleNamespace

import compile_controller as controller
import compile_memory as compiler
import compile_progress as progress
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _run
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _request(**changes):
    return {'schema': 'compile-request/v1', 'file': None,
            'closed_days_only': False, 'trigger': 'manual', **changes}


def _requested_work(work, request):
    queue = work[0]
    queue.cancel(work[1].id)
    queue.enqueue('compile', 1, {'compile_request': request})
    return queue, queue.claim('requested-compile'), work[2]


def test_request_options_reach_the_leased_executor(work, runtime, monkeypatch):
    request = _request(file='knowledge/daily/2026-07-14.md', closed_days_only=True)
    requested = _requested_work(work, request)
    observed = []
    monkeypatch.setattr(compiler, '_compile_under_lock', lambda args, **kwargs: observed.append(args) or 0)
    assert _run(requested)
    assert observed[0].file == str(compiler.ROOT / request['file'])
    assert observed[0].closed_days_only is True
    assert observed[0].trigger == 'manual'
    assert observed[0].compile_request == request


@pytest.mark.parametrize('document', [
    None, {}, _request(schema='future'), _request(extra=True),
    _request(trigger='unknown'), _request(trigger=True),
    _request(closed_days_only=1), _request(closed_days_only='yes'),
    _request(file='../outside.md'), _request(file='/tmp/2026-07-14.md'),
    _request(file='knowledge/daily/2026-13-01.md'),
    _request(file='knowledge/daily/../daily/2026-07-14.md'),
])
def test_invalid_selection_is_refused_before_lock_or_model(work, runtime, monkeypatch, document):
    requested = _requested_work(work, document)
    observed = []
    monkeypatch.setattr(compiler, '_compile_under_lock', lambda *args, **kwargs: observed.append(True) or 0)
    with pytest.raises(ValueError, match='compile request'):
        _run(requested)
    assert observed == []
    assert runtime == []


def _stored_request(work):
    queue, lease, _ = work
    snapshot = queue.get_compile_event(progress.root_work_id(queue.compile_origin(lease)), sequence=1)
    return controller.read_compile_execution(queue, lease, snapshot)[0]


@pytest.mark.parametrize('selection', [
    {'file': 'knowledge/daily/2026-07-14.md'},
    {'closed_days_only': True},
])
def test_actual_capture_uses_selected_sources_and_binds_request(work, runtime, monkeypatch, selection):
    (compiler.DAILY_DIR / '2026-07-15.md').write_text('New open day must remain pending.\n')
    request = _request(**selection)
    requested = _requested_work(work, request)
    observed = []
    select = compiler.select_dailies

    def record(*args, **kwargs):
        paths = select(*args, **kwargs)
        observed.extend(path.name for path in paths)
        return paths

    monkeypatch.setattr(compiler, 'select_dailies', record)
    assert _run(requested)
    assert observed == ['2026-07-14.md']
    assert len(runtime) == 3
    retained = _stored_request(requested)
    assert retained['schema'] == 'leased-compile-request/v2'
    assert retained['selection'] == request


def test_explicit_file_precedence_and_recovery_without_live_file(work, runtime, monkeypatch):
    request = _request(file='knowledge/daily/2026-07-14.md', closed_days_only=True)
    requested = _requested_work(work, request)
    assert _run(requested)
    (compiler.ROOT / request['file']).unlink()
    monkeypatch.setattr(compiler, 'select_dailies', lambda *a, **kw: pytest.fail('must use saved snapshot'))
    assert _run(requested)
    assert len(runtime) == 3
    assert _stored_request(requested)['selection'] == request


def test_payload_builder_uses_logical_path_and_excludes_lock_token(runtime):
    args = SimpleNamespace(file=str(compiler.DAILY_DIR / '2026-07-14.md'),
                           closed_days_only=True, trigger='manual', lock_token='ephemeral')
    assert compiler.compile_task_payload(args) == {
        'compile_request': _request(file='knowledge/daily/2026-07-14.md', closed_days_only=True)}


def test_dry_run_cannot_be_serialized_as_mutating_task(runtime):
    args = SimpleNamespace(file=None, closed_days_only=False, trigger='manual', dry_run=True)
    with pytest.raises(ValueError, match='dry-run'):
        compiler.compile_task_payload(args)
