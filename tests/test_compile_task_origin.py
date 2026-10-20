"""A redrive must resume original work before rereading changed live inputs."""
import sqlite3
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_protected_dispatch import provider as provider


@pytest.fixture
def leased(upgraded):
    root, state, _ = upgraded
    queue = memory_queue.active_memory_queue(root, state)
    return queue, queue.claim('origin-test')


def _prepare():
    return 'a' * 64, 'b' * 64, {'closed_days_only': True}


def _forbidden_prepare():
    pytest.fail('recovery reread current inputs')


def _redrive(queue, lease):
    queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
    queue.redrive(lease.id)
    return queue.claim('origin-redrive')


@pytest.mark.parametrize('links', [ [('a', 'a')], [('a', 'b'), ('b', 'a')], [('a', 'missing')] ])
def test_shared_lineage_reader_rejects_corruption(links):
    with sqlite3.connect(':memory:') as database:
        database.row_factory = sqlite3.Row
        database.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, redrive_of TEXT)')
        database.executemany('INSERT INTO tasks VALUES (?, ?)', links)
        with pytest.raises(memory_queue.QueueOperationError, match='redrive_lineage'):
            memory_queue._redrive_ancestors(database, 'a')


def test_compile_origin_is_stable_through_redrive(leased):
    queue, first = leased
    assert queue.compile_origin(first) == first.id
    second = _redrive(queue, first)
    assert queue.compile_origin(second) == first.id


def test_origin_requires_current_lease(leased):
    queue, lease = leased
    with pytest.raises(memory_queue.LeaseFenceError):
        queue.compile_origin(replace(lease, token='wrong'))


def test_root_recovery_does_not_recapture_changed_live_inputs(leased):
    queue, lease = leased
    first = controller.begin_compile_work(queue, lease, _prepare)
    assert first.source_digest == 'a' * 64
    assert controller.begin_compile_work(queue, lease, _forbidden_prepare) == first
    replacement = _redrive(queue, lease)
    assert controller.begin_compile_work(queue, replacement, _forbidden_prepare) == first


def test_origin_refuses_changed_ancestor_payload(leased):
    queue, lease = leased
    replacement = _redrive(queue, lease)
    with queue.connection() as database:
        database.execute('UPDATE tasks SET payload_blob=? WHERE id=?', (b'{}', lease.id))
    with pytest.raises(memory_queue.QueueOperationError, match='redrive_lineage'):
        queue.compile_origin(replacement)


def test_lost_lease_during_preparation_cannot_create_root(leased):
    queue, lease = leased

    def prepare():
        queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
        return _prepare()

    with pytest.raises(memory_queue.LeaseFenceError):
        controller.begin_compile_work(queue, lease, prepare)
    assert queue.get_compile_event(controller.progress.root_work_id(lease.id)) is None


@pytest.mark.parametrize('operation', ['event', 'writer', 'recovery'])
def test_root_cannot_be_recorded_under_a_different_origin(leased, operation):
    queue, lease = leased
    first = controller.begin_compile_work(queue, lease, _prepare)
    details = controller._compile_root_details('foreign', *_prepare())
    forged = replace(first, work_id=controller.progress.root_work_id('foreign'),
                     details={'compile_work': details})
    operations = {
        'event': lambda: queue.record_compile_event(lease, forged),
        'writer': lambda: queue.save_compile_artifact_writer(lease, forged, lambda out: out.write(b'bytes')),
        'recovery': lambda: queue.recover_compile_artifact_stream(lease, forged, expected_digest='a' * 64),
    }
    before = set(queue.results_dir.glob('*.result'))
    with pytest.raises(controller.progress.ProgressConflict, match='origin'):
        operations[operation]()
    assert queue.get_compile_event(forged.work_id) is None
    assert set(queue.results_dir.glob('*.result')) == before


def test_recorded_root_semantic_identity_is_verified_before_recovery(leased, monkeypatch):
    queue, lease = leased
    first = controller.begin_compile_work(queue, lease, _prepare)
    damaged = replace(first, details={'compile_work': {
        **first.details['compile_work'], 'dependency_digest': 'e' * 64,
    }})
    monkeypatch.setattr(queue, 'get_compile_event', lambda *a, **k: damaged)
    with pytest.raises(controller.progress.ProgressCorrupt, match='semantic identity'):
        controller.begin_compile_work(queue, lease, _forbidden_prepare)


def test_root_recovery_preserves_unknown_call_without_recapture_or_repeat(
    leased, provider, monkeypatch,
):
    import llm_client

    queue, lease = leased
    dependencies = controller.CompileDependencies.capture(lambda: {'fixture': True})
    first = controller.begin_compile_work(
        queue, lease, lambda: ('a' * 64, dependencies.digest, {'prompt': 'task'}),
    )
    checkpoint = queue.save_compile_artifact(
        lease, replace(first, sequence=1, stage='snapshot_ready'), b'snapshot fixture',
    )
    calls = []

    def interrupted(*args):
        calls.append(True)
        raise SystemExit('interrupted after dispatch')

    monkeypatch.setitem(llm_client._BACKENDS, 'fake', interrupted)
    with pytest.raises(SystemExit):
        controller.run_candidate_call(queue, lease, checkpoint, dependencies=dependencies,
                                      descriptor=provider, prompt='task', system_prompt='system')
    replacement = _redrive(queue, lease)
    assert controller.begin_compile_work(queue, replacement, _forbidden_prepare) == first
    with pytest.raises(controller.ProviderOutcomeUnknown):
        controller.run_candidate_call(queue, replacement, checkpoint, dependencies=dependencies,
                                      descriptor=provider, prompt='task', system_prompt='system')
    assert calls == [True]
    assert queue.get(replacement.id).state == 'blocked'


def test_saved_lineage_reader_has_no_redrive_depth_cutoff():
    with sqlite3.connect(':memory:') as database:
        database.row_factory = sqlite3.Row
        database.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, redrive_of TEXT)')
        database.executemany('INSERT INTO tasks VALUES (?, ?)',
                             [('first', None), ('second', 'first'), ('third', 'second')])
        assert memory_queue._redrive_lineage(database, 'third') == ('third', 'second', 'first')


def test_independent_tasks_do_not_share_root_even_with_identical_inputs(leased):
    queue, first_lease = leased
    first = controller.begin_compile_work(queue, first_lease, _prepare)
    queue.enqueue('compile', 1, {'daily': 'independent'})
    other_lease = queue.claim('independent-worker')
    other = controller.begin_compile_work(queue, other_lease, _prepare)
    assert other.work_id != first.work_id
    assert other.details['compile_work']['identity'] != first.details['compile_work']['identity']


def test_noncompile_task_cannot_start_compile_work(leased):
    queue, _ = leased
    queue.enqueue('query', 1, {'prompt': 'fixture'})
    other = queue.claim('query-worker')
    with pytest.raises(controller.progress.ProgressConflict, match='only compile'):
        controller.begin_compile_work(queue, other, _forbidden_prepare)


def test_original_root_restores_streamed_capture_after_live_sources_change(work, compiler_case):
    queue, lease, _ = work
    compiler, inputs, _ = compiler_case
    paths = sorted({compiler.ROOT / part.logical_path for part in inputs.dailies})
    first = controller.begin_compile_work(
        queue, lease, lambda: (controller.compile_capture_digest(paths), 'b' * 64, {}),
    )
    ready = replace(first, sequence=1, stage='snapshot_ready')
    saved = controller.save_compile_capture(queue, lease, ready, paths)
    replacement = _redrive(queue, lease)
    paths[0].write_bytes(b'live sources changed')
    assert controller.begin_compile_work(queue, replacement, _forbidden_prepare) == first
    resumed = replace(ready, task_id=replacement.id)
    assert controller.save_compile_capture(queue, replacement, resumed, paths) == saved
