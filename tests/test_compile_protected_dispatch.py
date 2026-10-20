"""Only protected, prepared calls cross the durable compile dispatch boundary."""
from dataclasses import replace

import compile_controller as controller
import llm_client as client
import pytest
from context_budget import TokenUsage
from model_dlp import DLPPolicy, DLPPolicyError

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    return replace(client.provider_candidates('fake')[0], model='test-model')


def _dependencies(seed='fixture'):
    return controller.CompileDependencies.capture(lambda: {'fixture': seed})


def _call(work, provider, **options):
    queue, lease, checkpoint = work
    return controller.run_candidate_call(
        queue, lease, checkpoint, dependencies=_dependencies(),
        descriptor=provider, prompt='private-literal task', system_prompt='system', **options,
    )


def test_protected_answer_survives_restart_without_provider_repetition(work, provider, monkeypatch):
    calls = []
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('private-literal',)))

    def backend(descriptor, prompt, system, schema):
        calls.append(prompt)
        return client.BackendResponse('safe answer', TokenUsage(input_tokens=11, output_tokens=2))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.usage.input_tokens == 11
    assert len(calls) == 1
    assert 'private-literal' not in calls[0]


def test_input_policy_failure_creates_no_dispatch(work, provider, monkeypatch):
    def broken():
        raise DLPPolicyError('required policy changed')

    monkeypatch.setattr(client, 'load_policy', broken)
    result = _call(work, provider)
    assert result.failure_class == 'dlp_policy_error'
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_timeout_blocks_instead_of_recording_completed_response(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        raise TimeoutError('potentially accepted upstream')

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(work, provider)
    queue, lease, checkpoint = work
    assert queue.get(lease.id).state == 'blocked'
    queue.unblock(lease.id)
    replacement = queue.claim('replacement')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call((queue, replacement, checkpoint), provider)
    assert calls == [True]


def test_output_policy_refusal_keeps_cost_without_persisting_secret(work, provider, monkeypatch):
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('private-literal',)))
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse('private-literal', TokenUsage(input_tokens=9, output_tokens=3))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.failure_class == 'dlp_output_blocked'
    assert first.usage.output_tokens == 3
    queue, _, checkpoint = work
    saved = queue.read_compile_artifact(queue.get_compile_event(checkpoint.work_id))
    assert b'private-literal' not in saved
    assert calls == [True]


def test_changed_model_cannot_reuse_previous_answer(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    with pytest.raises(ValueError, match='identity'):
        _call(work, replace(provider, model='another-model'))


def test_changed_policy_cannot_reuse_answer_from_previous_policy(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('new-private-word',)))
    with pytest.raises(ValueError, match='identity'):
        _call(work, provider)


def test_implicit_model_refuses_before_reservation(work, provider):
    with pytest.raises(ValueError, match='explicit resolved model'):
        _call(work, replace(provider, model=None))
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_unavailable_provider_creates_no_dispatch(work, provider, monkeypatch):
    monkeypatch.setattr(client, 'probe_candidate', lambda descriptor: False)
    result = _call(work, provider)
    assert result.failure_class == 'unavailable'
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_changed_dependency_identity_cannot_reuse_answer(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    queue, lease, checkpoint = work
    with pytest.raises(ValueError, match='identity'):
        controller.run_candidate_call(
            queue, lease, checkpoint, dependencies=_dependencies('changed'),
            descriptor=provider, prompt='private-literal task', system_prompt='system',
        )


def test_empty_completed_answer_retains_usage_and_is_not_called_again(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse('', TokenUsage(input_tokens=8, output_tokens=0))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.failure_class == 'empty_response'
    assert second.usage.input_tokens == 8
    assert calls == [True]


def test_replay_reads_earlier_answer_after_later_calls(work, provider, monkeypatch):
    calls = []

    def backend(descriptor, prompt, system, schema):
        calls.append(prompt)
        return client.BackendResponse(prompt, TokenUsage(input_tokens=1, output_tokens=1))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependencies=_dependencies())
    assert first(provider, 'draft', 'system').text == 'draft'
    assert first(provider, 'review', 'system').text == 'review'
    resumed = controller.CompileReplay(*work, dependencies=_dependencies())
    assert resumed(provider, 'draft', 'system').text == 'draft'
    assert resumed(provider, 'review', 'system').text == 'review'
    assert calls == ['draft', 'review']


def test_replay_refuses_changed_order_before_another_call(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        return 'answer'

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependencies=_dependencies())
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    resumed = controller.CompileReplay(*work, dependencies=_dependencies())
    with pytest.raises(ValueError, match='identity'):
        resumed(provider, 'different first request', 'system')
    assert len(calls) == 2


@pytest.fixture
def compiler_case(work, monkeypatch):
    import compile_memory as compiler

    from tests.test_compile_critic_drop_cannot_complete_source_work import _inputs

    queue, _, _ = work
    root = queue.vault
    state = queue.db_path.parent.parent
    for relative in ('knowledge/daily/receipts', 'knowledge/notes'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in ('knowledge/index.md', 'knowledge/log.md', 'AGENTS.md'):
        (root / relative).write_text('# Test snapshot\n')
    paths = {'ROOT': root, 'STATE_ROOT': state, 'MEMORY': root / 'knowledge',
             'DAILY_DIR': root / 'knowledge/daily', 'KNOWLEDGE': root / 'knowledge/notes',
             'INDEX': root / 'knowledge/index.md', 'LOG': root / 'knowledge/log.md',
             'AGENTS': root / 'AGENTS.md'}
    for name, value in paths.items():
        monkeypatch.setattr(compiler, name, value)
    return compiler, _inputs(root), state


@pytest.mark.parametrize('snapshot_mode', ['eager', 'streamed'])
def test_real_compile_validation_resumes_saved_draft_without_new_draft_call(
    work, compiler_case, monkeypatch, snapshot_mode,
):
    import json

    from compile_cache import CompileCache
    from markdown_transaction import active_markdown_coordinator

    from tests.test_compile_critic_drop_cannot_complete_source_work import _proposed
    from tests.test_the_compile_decides_what_the_snapshot_already_knows import (
        _provider,
        _source_review,
    )

    compiler, inputs, state = compiler_case
    work = _replay_capture(work, inputs, snapshot_mode)
    provider = _provider()
    queue, _, _ = work
    coordinator = active_markdown_coordinator(queue.vault, state)
    monkeypatch.setattr(compiler, '_compile_candidate_chain', lambda batch: [provider])
    monkeypatch.setattr(compiler, 'probe_candidate', lambda descriptor: True)
    replies = iter((json.dumps({'operations': [_proposed()]}),
                    json.dumps({'reviews': [{'slug': 'cedar-merge-order', 'verdict': 'pass',
                                             'reason': 'Supported by source.'}]}), _source_review()))
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse(next(replies), TokenUsage(input_tokens=7, output_tokens=3))

    def interrupted(*args):
        raise SystemExit('process stopped after durable draft answer, before validation')

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    with controller.open_compile_inputs(*work) as snapshot:
        inputs = _replay_inputs(snapshot, snapshot_mode)
        with monkeypatch.context() as patch:
            patch.setattr(compiler._CompileAttempt, '_planned', interrupted)
            with pytest.raises(SystemExit):
                compiler.resolve_compile_plan(
                    inputs, CompileCache(state), coordinator=coordinator,
                    call_runner=controller.CompileReplay(*work, dependencies=_dependencies()),
                )
    with controller.open_compile_inputs(*work) as snapshot:
        result = compiler.resolve_compile_plan(
            _replay_inputs(snapshot, snapshot_mode), CompileCache(state), coordinator=coordinator,
            call_runner=controller.CompileReplay(*work, dependencies=_dependencies()),
        )
    assert result.plan['operations'][0]['path'] == 'knowledge/notes/cedar-merge-order.md'
    assert len(calls) == 3  # One draft, operation review and source-completeness review.


def test_preflight_refusal_during_replay_does_not_skip_saved_call(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    controller.CompileReplay(*work, dependencies=_dependencies())(provider, 'draft', 'system')
    resumed = controller.CompileReplay(*work, dependencies=_dependencies())
    monkeypatch.setattr(client, 'probe_candidate', lambda descriptor: False)
    result = resumed(provider, 'draft', 'system')
    assert result.failure_class == 'unavailable'
    assert resumed.checkpoint == work[2]


def test_replay_historical_answer_still_requires_current_lease(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    first = controller.CompileReplay(*work, dependencies=_dependencies())
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    queue, lease, checkpoint = work
    import memory_queue

    stale = controller.CompileReplay(queue, replace(lease, token='stale'), checkpoint,
                                    dependencies=_dependencies())
    with pytest.raises(memory_queue.LeaseFenceError):
        stale(provider, 'draft', 'system')


def test_redriven_replay_reuses_all_saved_calls(work, provider, monkeypatch):
    import memory_queue

    calls = []

    def backend(*args):
        calls.append(True)
        return 'answer'

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependencies=_dependencies())
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    queue, lease, checkpoint = work
    queue.fail(lease, memory_queue.QueueFailure('handler_failed', permanent=True))
    queue.redrive(lease.id)
    replacement = queue.claim('redriven')
    resumed = controller.CompileReplay(queue, replacement, checkpoint, dependencies=_dependencies())
    resumed(provider, 'draft', 'system')
    resumed(provider, 'review', 'system')
    assert calls == [True, True]


def test_historical_read_checks_sequence_and_original_digest(work):
    import compile_progress

    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence) == checkpoint
    assert queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1) is None
    with pytest.raises(ValueError, match='nonnegative'):
        queue.get_compile_event(checkpoint.work_id, sequence=-1)
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute('UPDATE compile_work_events SET details_json=? WHERE sequence=?',
                         ('{}', checkpoint.sequence))
    with pytest.raises(compile_progress.ProgressCorrupt):
        queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence)


def test_snapshot_restores_original_bytes_projection_and_provenance(compiler_case, work):
    import hashlib

    compiler, inputs, _ = compiler_case
    raw = b'original\xff bytes'
    daily = replace(inputs.dailies[0], original_content=raw,
                    original_sha256=hashlib.sha256(raw).hexdigest(),
                    original_entries=(('12:34:56', 0, len(raw)),),
                    native_frames=(compiler.NativeCompileFrame('journal', 0, 3, '12:34:56', 'e\u0301', 'e\u0301'),))
    source = compiler.SourceSnapshot('knowledge/notes/example.md', raw,
                                    hashlib.sha256(raw).hexdigest(), b'projected')
    captured = replace(inputs, dailies=(daily,), sources=(source,), vault_files=(source,),
                       partition_context=object())
    saved_work = _archive_work(work, captured)
    restored = controller.load_compile_inputs(*saved_work)
    assert restored == captured
    assert restored.partition_context is None
    assert restored.sources[0].content == raw
    assert controller.compile_inputs_digest(restored) == controller.compile_inputs_digest(captured)


def test_snapshot_refuses_content_with_stale_hash(compiler_case):
    _, inputs, _ = compiler_case
    bad = replace(inputs, dailies=(replace(inputs.dailies[0], content=b'changed'),))
    with pytest.raises(ValueError, match='digest'):
        controller.compile_inputs_digest(bad)


def _archive_members(inputs):
    import io
    import zipfile

    handle = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller._write_archive_snapshot(inputs, digest, handle)
    with zipfile.ZipFile(handle) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _read_modified_archive(inputs, members):
    import hashlib
    import io
    import zipfile

    handle = io.BytesIO()
    with zipfile.ZipFile(handle, 'w') as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with zipfile.ZipFile(handle) as archive:
        snapshot = controller.CompileInputArchive(archive, hashlib.sha256(members['manifest.json']).hexdigest())
        return snapshot.materialize()


def test_snapshot_refuses_tampered_blob(compiler_case):
    _, inputs, _ = compiler_case
    members = _archive_members(inputs)
    key = next(name for name in members if name.startswith('blobs/'))
    members[key] = b'altered'
    with pytest.raises(ValueError, match='digest'):
        _read_modified_archive(inputs, members)


@pytest.mark.parametrize('mutation', ['unknown', 'noncanonical'])
def test_snapshot_refuses_unknown_fields_and_noncanonical_data(compiler_case, mutation):
    import json

    _, inputs, _ = compiler_case
    members = _archive_members(inputs)
    if mutation == 'noncanonical':
        members['manifest.json'] += b'\n'
    else:
        value = json.loads(members['manifest.json'])
        value['records']['future_field'] = 'must not silently lose'
        members['manifest.json'] = controller._snapshot_json(value)
    with pytest.raises(ValueError):
        _read_modified_archive(inputs, members)


def test_snapshot_survives_queue_reopen_without_reading_sources(compiler_case, work):
    import compile_progress as progress
    import memory_queue

    _, inputs, _ = compiler_case
    queue, lease, _ = work
    digest = controller.compile_inputs_digest(inputs)
    event = progress.CompileEvent('a' * 64, lease.id, 0, digest, 'pending', 'ready', {})
    queue.record_compile_event(lease, event)
    saved = controller.save_compile_inputs(queue, lease, replace(event, sequence=1,
                                            stage='snapshot_ready'), inputs)
    reopened = memory_queue.active_memory_queue(queue.vault, queue.db_path.parent.parent)
    assert controller.load_compile_inputs(reopened, lease, saved) == inputs
    wrong = replace(saved, source_digest='b' * 64)
    with pytest.raises(ValueError):
        controller.load_compile_inputs(reopened, lease, wrong)


def test_snapshot_rejects_changed_digest_before_artifact_write(compiler_case, work):
    _, inputs, _ = compiler_case
    queue, lease, checkpoint = work
    with pytest.raises(ValueError, match='digest'):
        controller.save_compile_inputs(queue, lease, checkpoint, inputs)
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def _captured_work(work, inputs):
    import compile_progress as progress

    queue, lease, _ = work
    digest = controller.compile_inputs_digest(inputs)
    event = progress.CompileEvent('9' * 64, lease.id, 0, digest, 'pending', 'ready', {})
    queue.record_compile_event(lease, event)
    saved = controller.save_compile_inputs(queue, lease, replace(event, sequence=1,
                                            stage='snapshot_ready'), inputs)
    return queue, lease, saved


def test_streamed_artifact_exceeds_small_result_limit_and_audits(work):
    import hashlib

    import compile_progress as progress

    queue, lease, checkpoint = work
    chunk = b'x' * (1024 * 1024)
    event = replace(checkpoint, sequence=2, details={})
    saved = queue.save_compile_artifact_stream(lease, event, (chunk for _ in range(20)))
    expected = hashlib.sha256(chunk * 20).hexdigest()
    assert saved.details['artifact']['sha256'] == expected
    assert queue.verify_compile_artifact(saved) == 20 * len(chunk)
    with queue._connect() as database:
        result = progress.audit_events(database, queue.verify_compile_artifact)
    assert result['artifacts'] == 2


def test_interrupted_stream_does_not_publish_or_leave_partial_file(work):
    queue, lease, checkpoint = work

    def interrupted():
        yield b'first bytes'
        raise OSError('disk exhausted')

    before = set(queue.results_dir.iterdir())
    with pytest.raises(OSError, match='disk exhausted'):
        queue.save_compile_artifact_stream(lease, replace(checkpoint, sequence=2, details={}), interrupted())
    assert set(queue.results_dir.iterdir()) == before
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_stream_conflicting_existing_artifact_is_not_overwritten(work):
    import memory_queue

    queue, lease, checkpoint = work
    event = replace(checkpoint, sequence=2, details={})
    saved = queue.save_compile_artifact_stream(lease, event, iter((b'original',)))
    with pytest.raises(memory_queue.QueueOperationError, match='durable_file_conflict'):
        queue.save_compile_artifact_stream(lease, event, iter((b'different',)))
    assert queue.read_compile_artifact(saved) == b'original'


def test_stream_verifier_refuses_mutated_artifact(work):
    queue, lease, checkpoint = work
    saved = queue.save_compile_artifact_stream(
        lease, replace(checkpoint, sequence=2, details={}), iter((b'original',)))
    path = queue.state_root / saved.details['artifact']['reference']
    path.write_bytes(b'mutated!')
    with pytest.raises(ValueError, match='digest'):
        queue.verify_compile_artifact(saved)


def test_stream_recovery_adopts_complete_file_after_event_commit_failure(work, monkeypatch):
    import hashlib

    queue, lease, checkpoint = work
    event = replace(checkpoint, sequence=2, details={})
    digest = hashlib.sha256(b'complete').hexdigest()

    def failed(*args):
        raise OSError('event commit unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', failed)
        with pytest.raises(OSError, match='event commit'):
            queue.save_compile_artifact_stream(lease, event, iter((b'complete',)))
    saved = queue.recover_compile_artifact_stream(lease, event, expected_digest=digest)
    assert queue.read_compile_artifact(saved) == b'complete'


def test_stream_writer_checks_lease_before_consuming_input(work):
    import memory_queue

    queue, lease, checkpoint = work

    def forbidden():
        pytest.fail('stale lease consumed source bytes')
        yield b'never'

    with pytest.raises(memory_queue.LeaseFenceError):
        queue.save_compile_artifact_stream(replace(lease, token='wrong'), checkpoint, forbidden())


def test_stream_verification_keeps_memory_below_artifact_size(work):
    import tracemalloc

    queue, lease, checkpoint = work
    block = b'x' * (1024 * 1024)
    saved = queue.save_compile_artifact_stream(
        lease, replace(checkpoint, sequence=2, details={}), (block for _ in range(20)))
    tracemalloc.start()
    try:
        assert queue.verify_compile_artifact(saved) == 20 * len(block)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < len(block)  # Even one MiB is above this verifier's fixed working set.


def test_stream_verification_honours_expired_deadline(work):
    queue, _, checkpoint = work
    with pytest.raises(TimeoutError):
        queue.verify_compile_artifact(checkpoint, deadline=0)


def test_stream_write_expired_deadline_leaves_no_artifact(work):
    queue, lease, checkpoint = work
    before = set(queue.results_dir.iterdir())
    with pytest.raises(TimeoutError):
        queue.save_compile_artifact_stream(
            lease, replace(checkpoint, sequence=2, details={}), iter((b'bytes',)), deadline=0)
    assert set(queue.results_dir.iterdir()) == before
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_stream_digest_reads_only_initial_extent_even_if_producer_never_ends():
    import io
    from unittest.mock import Mock

    import memory_queue

    handle = Mock()
    handle.read.side_effect = lambda size: b'x' * size
    _, size = memory_queue._stream_digest(handle, io.DEFAULT_BUFFER_SIZE * 3)
    assert size == io.DEFAULT_BUFFER_SIZE * 3
    assert handle.read.call_count == 3


def test_archive_snapshot_reads_selected_record_without_loading_other_bodies(compiler_case, work):
    import hashlib

    compiler, inputs, _ = compiler_case
    large = b'z' * (20 * 1024 * 1024)
    source = compiler.SourceSnapshot('knowledge/notes/large.md', large,
                                    hashlib.sha256(large).hexdigest())
    inputs = replace(inputs, sources=(source,), vault_files=(source,))
    saved_work = _archive_work(work, inputs)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        assert snapshot.record_count('sources') == 1
        assert snapshot.record_metadata('sources', 0)['logical_path'] == source.logical_path
        assert snapshot.record('sources', 0) == source
        assert snapshot.materialize() == inputs


def _archive_work(work, inputs):
    import compile_progress as progress

    queue, lease, _ = work
    event = progress.CompileEvent('8' * 64, lease.id, 0,
                                  controller.compile_inputs_digest(inputs), 'pending', 'ready', {})
    queue.record_compile_event(lease, event)
    saved = controller.save_compile_inputs(queue, lease, replace(event, sequence=1,
                                            stage='snapshot_ready'), inputs)
    return queue, lease, saved


def test_archive_snapshot_open_does_not_load_blob_members(compiler_case, work, monkeypatch):
    import zipfile

    _, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    original = zipfile.ZipFile.read

    def guarded(archive, name, *args, **kwargs):
        assert not str(name).startswith('blobs/')
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'read', guarded)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        assert snapshot.record_count('dailies') == len(inputs.dailies)
        assert snapshot.record_metadata('dailies', 0)['sha256'] == inputs.dailies[0].sha256


def test_archive_snapshot_reader_cannot_outlive_context(compiler_case, work):
    _, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        assert snapshot.record_count('dailies')
    with pytest.raises(ValueError, match='closed'):
        snapshot.record('dailies', 0)


def test_archive_snapshot_retry_after_event_failure_is_byte_identical(compiler_case, work, monkeypatch):
    import compile_progress as progress

    _, inputs, _ = compiler_case
    queue, lease, _ = work
    pending = progress.CompileEvent('8' * 64, lease.id, 0,
                                   controller.compile_inputs_digest(inputs), 'pending', 'ready', {})
    queue.record_compile_event(lease, pending)
    ready = replace(pending, sequence=1, stage='snapshot_ready')

    def interrupted(*args):
        raise RuntimeError('event commit failed')

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupted)
        with pytest.raises(RuntimeError, match='event commit failed'):
            controller.save_compile_inputs(queue, lease, ready, inputs)
    saved = controller.save_compile_inputs(queue, lease, ready, inputs)
    assert controller.load_compile_inputs(queue, lease, saved) == inputs


def test_archive_snapshot_restoration_rejects_original_digest_mismatch(compiler_case):
    compiler, inputs, _ = compiler_case
    from dataclasses import asdict

    value = asdict(inputs.dailies[0])
    value.update(original_content=b'changed', original_sha256='0' * 64)
    with pytest.raises(ValueError, match='digest mismatch'):
        controller._restored_archive_record(compiler.DailySnapshot, value)


@pytest.mark.parametrize('records', [None, [], {'dailies': {}, 'sources': [],
                                              'targets': [], 'vault_files': []}])
def test_archive_snapshot_rejects_malformed_record_lists(records):
    import hashlib
    import io
    import zipfile

    import compile_progress as progress

    data = controller._snapshot_json({'schema': controller._ARCHIVE_SCHEMA, 'records': records})
    handle = io.BytesIO()
    with zipfile.ZipFile(handle, 'w') as archive:
        archive.writestr('manifest.json', data)
    with zipfile.ZipFile(handle) as archive:
        with pytest.raises(progress.ProgressCorrupt):
            controller.CompileInputArchive(archive, hashlib.sha256(data).hexdigest())


def test_open_compile_artifact_rejects_replacement_during_read(work):
    import memory_queue

    queue, _, checkpoint = work
    path = queue.state_root / checkpoint.details['artifact']['reference']
    with pytest.raises(memory_queue.QueueOperationError):
        with queue.open_compile_artifact(checkpoint) as handle:
            assert handle.read(1)
            path.write_bytes(b'changed')


def test_open_compile_artifact_refuses_symlink(work, tmp_path):
    import memory_queue

    queue, _, checkpoint = work
    path = queue.state_root / checkpoint.details['artifact']['reference']
    target = tmp_path / 'elsewhere'
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(memory_queue.QueueOperationError):
        with queue.open_compile_artifact(checkpoint):
            pytest.fail('symlink artifact was opened')


def test_archive_planning_keeps_context_records_lazy(compiler_case, work, monkeypatch):
    compiler, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        planned = snapshot.planning_inputs()
        assert planned.dailies == inputs.dailies
        original = snapshot.record
        reads = []

        def observed(group, index):
            reads.append((group, index))
            return original(group, index)

        monkeypatch.setattr(snapshot, 'record', observed)
        selected = compiler._context_sources(planned, set(), {'absent'})
        assert selected == ()
        assert reads == []
        assert tuple(planned.targets) == inputs.targets


def test_archive_planning_batches_match_eager_snapshot(compiler_case, work):
    compiler, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    expected = compiler.pack_compile_batches(inputs, model=None)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        actual = compiler.pack_compile_batches(snapshot.planning_inputs(), model=None)
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected):
            assert left.manifest == right.manifest
            assert left.packing == right.packing
            assert left.inputs.sources == right.inputs.sources
            assert tuple(left.inputs.targets) == right.inputs.targets
            assert left.required_context_paths == right.required_context_paths


def test_context_ranking_does_not_keep_source_bodies(compiler_case):
    compiler, inputs, _ = compiler_case
    import weakref

    source = compiler.SourceSnapshot('knowledge/notes/temporary.md', b'temporary body', 'd' * 64)
    observed = weakref.ref(source)
    sources = [source]
    ranking = compiler._ContextRanking(sources)
    sources.clear()
    del source
    assert observed() is None
    assert ranking.sources is sources


def test_archive_byte_measure_indexes_metadata_before_body_reads(compiler_case, work, monkeypatch):
    compiler, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        planned = snapshot.planning_inputs()

        def forbidden(*args):
            pytest.fail('byte measure initialization loaded context bodies')

        monkeypatch.setattr(snapshot, 'record', forbidden)
        measure = compiler._ByteBatchMeasure(planned)
        assert measure.context


@pytest.mark.parametrize('context_pending', [False, True])
def test_archive_planning_preserves_required_citations(compiler_case, work, context_pending):
    from tests.test_compile_keeps_physically_overlapping_existing_context import _overlap_inputs

    compiler, _, _ = compiler_case
    inputs, original = _overlap_inputs(work[0].vault)
    saved_work = _archive_work(work, inputs)
    expected = compiler._pack_compile_batches(inputs, model=None, context_pending=context_pending)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        actual = compiler._pack_compile_batches(snapshot.planning_inputs(), model=None,
                                                context_pending=context_pending)
        assert len(actual) == len(expected)
        assert actual[0].inputs.sources == expected[0].inputs.sources
        assert actual[0].packing == expected[0].packing
        assert actual[0].required_context_paths == expected[0].required_context_paths
        sources = {source.logical_path: source.content for source in actual[0].inputs.sources}
        assert sources['knowledge/notes/project-rule.md'] == original


def test_archive_byte_size_cache_keeps_no_decoded_context(compiler_case, work, monkeypatch):
    import weakref

    compiler, inputs, _ = compiler_case
    saved_work = _archive_work(work, inputs)
    with controller.open_compile_inputs(*saved_work) as snapshot:
        planned = snapshot.planning_inputs()
        original = snapshot.record
        observed = []

        def track(group, index):
            record = original(group, index)
            observed.append(weakref.ref(record))
            return record

        monkeypatch.setattr(snapshot, 'record', track)
        measure = compiler._ByteBatchMeasure(planned)
        path = next(iter(measure.context))
        assert measure._context_size(path) > 0
        assert observed and all(ref() is None for ref in observed)
        first_count = len(observed)
        assert measure._context_size(path) > 0
        assert len(observed) == first_count


def _replay_capture(work, inputs, mode):
    if mode == 'eager':
        return _captured_work(work, inputs)
    import compile_progress

    queue, lease, _ = work
    paths = sorted({queue.vault / part.logical_path for part in inputs.dailies})
    event = compile_progress.CompileEvent('7' * 64, lease.id, 0,
                                          controller.compile_capture_digest(paths), 'pending', 'ready', {})
    queue.record_compile_event(lease, event)
    saved = controller.save_compile_capture(queue, lease,
                                            replace(event, sequence=1, stage='snapshot_ready'), paths)
    return queue, lease, saved


def _replay_inputs(snapshot, mode):
    if mode == 'eager':
        return snapshot.planning_inputs()
    batches = tuple(snapshot.iter_batches(model=None))
    assert len(batches) == 1
    return batches[0].inputs
