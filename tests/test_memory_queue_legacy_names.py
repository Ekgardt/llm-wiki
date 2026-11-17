"""The queue's compatibility names: re-exports and the heartbeat, timing, process-cleanup and child
runtime names are the owners' objects, and contract classes and values keep their legacy pickle bytes,
also across a real spawn.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import queue_core  # noqa: E402
from llm_wiki import (
    queue_store,  # noqa: E402
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))



def test_payload_format_public_reexports_are_the_same_objects():
    from llm_wiki.formats.queue_payload import PayloadValidation, validate_payload_blob

    assert queue_core.PayloadValidation is PayloadValidation
    assert queue_core.validate_payload_blob is validate_payload_blob


def _legacy_contract_pickle(kind):
    """Read only the digest-bound, repository-owned compatibility fixture."""
    import base64

    fixture = Path(__file__).parent / 'modules/queue_contracts/fixtures/legacy-pickle.json'
    metadata = json.loads(fixture.read_text(encoding='utf-8'))
    blob = base64.b64decode(metadata[{'class': 'classes_base64', 'value': 'values_base64'}[kind]], validate=True)
    assert hashlib.sha256(blob).hexdigest() == metadata[f'{kind}_blob_sha256']
    return metadata, blob


def _contract_baseline_values():
    from llm_wiki import queue_contracts as contracts

    return [contracts.QueueFailure('temporary', False, None, 3.5),
            contracts.DeferredResult(b'complete'),
            contracts.QueueOperationError('baseline', 'detail'),
            contracts.CompileCompletionError('incomplete', 'baseline'),
            contracts.WorkerSummary(2, 1, 1, 0, 0, 3)]


def test_queue_contract_classes_keep_identical_legacy_pickle_bytes():
    import pickle

    from llm_wiki import queue_contracts as contracts

    metadata, blob = _legacy_contract_pickle('class')
    classes = [getattr(contracts, name) for name in metadata['names']]
    assert pickle.loads(blob) == classes
    assert classes == [getattr(queue_core, name, cls) for name, cls in zip(metadata['names'], classes)]
    assert pickle.dumps(classes, protocol=metadata['protocol']) == blob


def test_queue_contract_values_keep_identical_legacy_pickle_bytes():
    import pickle

    metadata, blob = _legacy_contract_pickle('value')
    assert pickle.dumps(_contract_baseline_values(), protocol=metadata['protocol']) == blob
    restored = pickle.loads(blob)
    assert restored[0] == _contract_baseline_values()[0]
    assert restored[1].data == b'complete'
    assert (restored[2].code, restored[2].detail) == ('baseline', 'detail')
    assert isinstance(restored[3], queue_core.CompileCompletionError)
    assert restored[4].remaining_eligible == 3


def _echo_contract_values(connection, values):
    try:
        connection.send(values)
    finally:
        connection.close()


def test_queue_contract_values_cross_a_real_spawn_in_both_directions():
    import multiprocessing
    import pickle

    from tests.slow_machine import LONG_TIMEOUT

    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_echo_contract_values, args=(sender, _contract_baseline_values()))
    process.start()
    sender.close()
    try:
        assert receiver.poll(LONG_TIMEOUT), f'child exited {process.exitcode}'
        received = receiver.recv()
        process.join(LONG_TIMEOUT)
        assert process.exitcode == 0
        metadata, blob = _legacy_contract_pickle('value')
        assert pickle.dumps(received, protocol=metadata['protocol']) == blob
    finally:
        if process.is_alive():
            process.terminate()
        process.join(LONG_TIMEOUT)
        receiver.close()


def test_heartbeat_compatibility_names_are_the_owner_classes():
    from llm_wiki import queue_heartbeat

    assert queue_core.LeaseHeartbeat is queue_heartbeat.LeaseHeartbeat
    assert queue_core.SourceFenceHeartbeat is queue_heartbeat.SourceFenceHeartbeat


def test_the_queue_reaches_timing_functions_as_the_owner_objects():
    from llm_wiki import queue_timing

    from tests.modules.queue_timing.boundary_helpers import timing_function_names

    reached = timing_function_names() & vars(queue_core).keys()
    assert {'parse_timestamp', 'retry_available_at', 'timestamp'} <= reached
    for name in reached:
        assert getattr(queue_core, name) is getattr(queue_timing, name)
    assert queue_store.utc_now.__module__ == 'llm_wiki.queue_store'


def test_queue_cleanup_names_reference_the_single_owner():
    import llm_wiki.queue_process_cleanup as cleanup

    names = ('pid_is_alive', 'taskkill', 'terminate_processor_child', 'tracked_descendant_pids')
    for name in names:
        assert getattr(queue_core, name, getattr(cleanup, name)) is getattr(cleanup, name)


def test_queue_child_names_and_result_limit_reference_their_owners():
    import llm_wiki.queue_child_runtime as child
    from llm_wiki.queue_contracts import MAX_RESULT_BYTES

    names = ('ChildRun', 'await_child_message', 'child_ready_handshake',
             'decode_processor_frame', 'settle_child_exit')
    for name in names:
        assert getattr(queue_core, name, getattr(child, name)) is getattr(child, name)
    assert queue_core.MAX_RESULT_BYTES == child.MAX_RESULT_BYTES == MAX_RESULT_BYTES
