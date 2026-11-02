"""Byte protocol, limits and discovery semantics without the queue executor."""
from types import SimpleNamespace

import llm_wiki.queue_child_runtime as child
import pytest
from llm_wiki.queue_contracts import DeferredResult, QueueOperationError


@pytest.mark.parametrize('value, frame', [
    (True, b'T'), (False, b'F'), (DeferredResult(b'abc'), b'Dabc'),
    (None, b'?'), (1, b'?'),
])
def test_encoder_retains_wire_bytes(value, frame):
    assert child._encode_processor_outcome(value) == frame


@pytest.mark.parametrize('frame, value', [(b'T', True), (b'F', False), (b'D', DeferredResult(b''))])
def test_decoder_retains_outcomes(frame, value):
    assert child._decode_processor_frame(frame) == value


@pytest.mark.parametrize('frame, code', [
    (b'E', 'processor_exception'), (b'', 'processor_result_malformed'),
    (b'Textra', 'processor_result_malformed'), (b'Eextra', 'processor_result_malformed'),
])
def test_decoder_rejects_unrecognized_whole_frames(frame, code):
    with pytest.raises(QueueOperationError) as raised:
        child._decode_processor_frame(frame)
    assert raised.value.code == code


def test_result_byte_ceiling_keeps_exact_boundary(monkeypatch):
    monkeypatch.setattr(child, '_MAX_RESULT_BYTES', 3)
    assert child._decode_processor_frame(b'Dabc').data == b'abc'
    with pytest.raises(QueueOperationError) as raised:
        child._decode_processor_frame(b'Dabcd')
    assert raised.value.code == 'processor_result_oversize'


@pytest.mark.parametrize('ready, ack, expected', [
    (False, b'A', [b'R']), (True, b'?', [b'R']), (True, b'A', [b'R', b'T']),
])
def test_sender_requires_parent_ack_before_result(ready, ack, expected):
    sent = []
    sender = SimpleNamespace(send_bytes=sent.append, poll=lambda timeout: ready,
                             recv_bytes=lambda limit: ack)
    child._send_processor_frame(sender, b'T')
    assert sent == expected


@pytest.mark.parametrize('tracked, expected', [(None, {}), (set(), {'tracked_descendants': set()})])
def test_stop_distinguishes_untracked_from_known_empty(monkeypatch, tracked, expected):
    calls = []

    def stop(process, **kwargs):
        calls.append((process, kwargs))
        return set()

    monkeypatch.setattr(child, '_terminate_processor_child', stop)
    process = object()
    run = child._ChildRun(process, None, 0, tracked_descendants=tracked)
    run.stop()
    assert calls == [(process, expected)]
    assert run.tracked_descendants == set()


def _must_not_start(*args, **kwargs):
    raise AssertionError('process start must not be attempted')


def test_cancelled_request_never_creates_process(monkeypatch):
    monkeypatch.setattr(child.multiprocessing, 'get_context', _must_not_start)
    with pytest.raises(QueueOperationError) as raised:
        child.run_processor_child(_must_not_start, {}, 1, cancelled=lambda: True,
                                  child_entry=_must_not_start)
    assert raised.value.code == 'worker_cancelled'


def test_expired_request_never_creates_process(monkeypatch):
    monkeypatch.setattr(child.multiprocessing, 'get_context', _must_not_start)
    with pytest.raises(TimeoutError):
        child.run_processor_child(_must_not_start, {}, 0, child_entry=_must_not_start)
