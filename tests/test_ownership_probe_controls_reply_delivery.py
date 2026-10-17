"""Ownership faults must not depend on a peer being slower than the probe."""
from __future__ import annotations

import time
from types import SimpleNamespace

from benchmark import run_code_navigation as runner
from tests.fake_lsp_server import FakeLspServer
from tests.slow_machine import SHORT_TIMEOUT


class _CompletedRequestRuntime(runner._RealNavigationRuntime):
    def _start_probe_request(self, *args, **kwargs):
        worker = super()._start_probe_request(*args, **kwargs)
        worker.join(SHORT_TIMEOUT)
        assert not worker.is_alive()
        return worker


def _immediate_peer(peer):
    request = peer.read()
    assert request['method'] == 'workspace/symbol'
    assert request['params'] == {'query': '__llm_wiki_ownership_probe_no_match__'}
    peer.send({'jsonrpc': '2.0', 'id': request['id'], 'result': []})


def test_a_fast_actual_framed_reply_cannot_race_the_controlled_timeout():
    server = FakeLspServer()
    protocol = server.start(_immediate_peer)
    runtime = object.__new__(_CompletedRequestRuntime)
    runtime._cleanup_failed = False
    process = SimpleNamespace(protocol=protocol, request=protocol.request)
    try:
        runtime._observe_inflight_interruption(process, 'timeout', time.monotonic()+SHORT_TIMEOUT)
    finally:
        server.close()
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()
    assert not server.failures
    assert not any(thread.is_alive() for thread in server.threads)


def test_a_normal_fast_framed_reply_is_still_delivered_unchanged():
    server = FakeLspServer()
    protocol = server.start(_immediate_peer)
    try:
        result=protocol.request('workspace/symbol', {'query':'__llm_wiki_ownership_probe_no_match__'}, deadline=time.monotonic()+SHORT_TIMEOUT)
        assert result == []
    finally:
        server.close()
    assert not server.failures


class _DeliveryProtocol:
    generation_nonce = "real-generation"

    def __init__(self):
        self.queued = []
        self.delivered = []

    def _sent_request_evidence(self):
        return 0, None

    def _queue_request_message(self, message, *, deadline, key):
        self.queued.append((message, deadline, key))

    def _handle_response(self, message, nonce):
        self.delivered.append((message, nonce))


def _probe_message(identifier=7):
    return dict(id=identifier, method='workspace/symbol',
                params={'query': '__llm_wiki_ownership_probe_no_match__'})


def _queue_probe(protocol, identifier=7):
    protocol._queue_request_message(_probe_message(identifier),
        deadline=time.monotonic()+SHORT_TIMEOUT,
        key=(protocol.generation_nonce, identifier))


def test_scope_holds_only_exact_nonce_and_id_and_replays_real_reply():
    protocol = _DeliveryProtocol()
    reply = dict(id=7, result=['unchanged'])
    with runner._ProbeReplyDelivery(protocol) as delivery:
        _queue_probe(protocol)
        protocol._handle_response(reply, 'real-generation')
        protocol._handle_response(dict(id=8, result=[]), 'real-generation')
        protocol._handle_response(reply, 'other-generation')
        assert protocol.delivered == [(dict(id=8, result=[]), 'real-generation'),
                                       (reply, 'other-generation')]
    assert protocol.delivered[-1] == (reply, 'real-generation')
    assert delivery.reply_count == 1
    assert delivery.released.is_set()
    assert not hasattr(protocol, '_benchmark_probe_delivery')
    assert '_queue_request_message' not in vars(protocol)
    assert '_handle_response' not in vars(protocol)


def test_other_parameters_are_never_held():
    protocol = _DeliveryProtocol()
    message = _probe_message()
    message['params'] = {'query': 'ordinary query'}
    with runner._ProbeReplyDelivery(protocol):
        protocol._queue_request_message(message, deadline=1, key=('real-generation', 7))
        protocol._handle_response(dict(id=7, result=[]), 'real-generation')
        assert protocol.delivered == [(dict(id=7, result=[]), 'real-generation')]


def test_nested_scope_refuses_and_preserves_outer_scope():
    import pytest
    protocol = _DeliveryProtocol()
    with runner._ProbeReplyDelivery(protocol) as outer:
        with pytest.raises(runner._OwnershipProbeError, match='overlap'):
            with runner._ProbeReplyDelivery(protocol):
                raise AssertionError('unreachable')
        assert protocol._benchmark_probe_delivery is outer
        _queue_probe(protocol)
    assert outer.released.is_set()


def test_duplicate_request_refuses_and_restores_handlers():
    import pytest
    protocol = _DeliveryProtocol()
    with pytest.raises(runner._OwnershipProbeError, match='multiple probes'):
        with runner._ProbeReplyDelivery(protocol):
            _queue_probe(protocol)
            _queue_probe(protocol, 9)
    assert len(protocol.queued) == 1
    assert not hasattr(protocol, '_benchmark_probe_delivery')


def test_duplicate_actual_replies_are_forwarded_then_refused():
    import pytest
    protocol = _DeliveryProtocol()
    reply = dict(id=7, result=[])
    with pytest.raises(runner._OwnershipProbeError, match='duplicate replies'):
        with runner._ProbeReplyDelivery(protocol):
            _queue_probe(protocol)
            protocol._handle_response(reply, 'real-generation')
            protocol._handle_response(reply, 'real-generation')
    assert protocol.delivered == [(reply, 'real-generation')] * 2
    assert '_handle_response' not in vars(protocol)


def test_original_exception_replays_and_restores_instance_overrides():
    import pytest
    protocol = _DeliveryProtocol()
    original = protocol._handle_response
    protocol._handle_response = original
    with pytest.raises(ValueError, match='original failure'):
        with runner._ProbeReplyDelivery(protocol):
            _queue_probe(protocol)
            protocol._handle_response(dict(id=7, result=[]), 'real-generation')
            raise ValueError('original failure')
    assert protocol._handle_response is original
    assert protocol.delivered == [(dict(id=7, result=[]), 'real-generation')]


def _failed_response(message, nonce):
    raise OSError('actual response callback failure')


def test_replay_failure_still_restores_scope_and_handlers():
    import pytest
    protocol = _DeliveryProtocol()
    protocol._handle_response = _failed_response
    with pytest.raises(OSError, match='callback failure'):
        with runner._ProbeReplyDelivery(protocol) as delivery:
            _queue_probe(protocol)
            protocol._handle_response(dict(id=7, result=[]), 'real-generation')
    assert protocol._handle_response is _failed_response
    assert not hasattr(protocol, '_benchmark_probe_delivery')
    assert delivery.released.is_set()
    assert delivery.deferred == []


def test_expired_deadline_refuses_without_dispatch_and_restores():
    import pytest
    protocol = _DeliveryProtocol()
    runtime = object.__new__(runner._RealNavigationRuntime)
    runtime._cleanup_failed = False
    process = SimpleNamespace(protocol=protocol)
    with pytest.raises(TimeoutError, match='deadline'):
        runtime._observe_inflight_interruption(process, 'timeout', time.monotonic()-1)
    assert protocol.queued == []
    assert not hasattr(protocol, '_benchmark_probe_delivery')


def test_a_real_framed_reply_is_deferred_until_actual_cancellation():
    server = FakeLspServer()
    protocol = server.start(_immediate_peer)
    runtime = object.__new__(runner._RealNavigationRuntime)
    runtime._cleanup_failed = False
    process = SimpleNamespace(protocol=protocol, request=protocol.request)
    try:
        runtime._observe_inflight_interruption(process, 'cancellation', time.monotonic()+SHORT_TIMEOUT)
    finally:
        server.close()
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()
    assert not server.failures
    assert not any(thread.is_alive() for thread in server.threads)
    assert runtime.ownership_reply_faults['cancellation']['released'] is True


def test_already_entered_callback_forwards_after_scope_release():
    protocol = _DeliveryProtocol()
    reply = dict(id=7, result=[])
    with runner._ProbeReplyDelivery(protocol):
        _queue_probe(protocol)
        retained_callback = protocol._handle_response
    retained_callback(reply, 'real-generation')
    assert protocol.delivered == [(reply, 'real-generation')]


def _failed_queue(message, *, deadline, key):
    raise OSError('actual queue failure')


def test_queue_failure_restores_without_claiming_dispatch():
    import pytest
    protocol = _DeliveryProtocol()
    protocol._queue_request_message = _failed_queue
    with pytest.raises(OSError, match='queue failure'):
        with runner._ProbeReplyDelivery(protocol) as delivery:
            _queue_probe(protocol)
    assert protocol._queue_request_message is _failed_queue
    assert protocol.queued == []
    assert delivery.reply_count == 0
    assert delivery.released.is_set()
