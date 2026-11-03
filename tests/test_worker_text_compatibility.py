"""Moved worker values and text helper preserve original import/pickle identities."""
import json
import pickle
from pathlib import Path

import code_navigation_renderer
import memory_queue
import pytest
from llm_wiki import queue_worker_state, utf8_text

ROOT = Path(__file__).resolve().parents[1]
LEGACY_WORKER = json.loads((ROOT / 'tests/modules/queue_worker_state/legacy-pickle.json').read_text())


def _worker_values():
    return [memory_queue.QueueRetryPolicy, memory_queue._ProcessorOutcome,
        memory_queue._WorkerProgress, memory_queue.processor_error_code,
        memory_queue.QueueRetryPolicy(), memory_queue._ProcessorOutcome(False, False, False),
        memory_queue._WorkerProgress({'ok': 0, 'failed': 0}), memory_queue._WorkerProgress.advance]


@pytest.mark.parametrize(('position', 'encoded'), enumerate(LEGACY_WORKER))
def test_worker_serialization_bytes_stay_identical(position, encoded):
    value = _worker_values()[position]
    assert pickle.dumps(value).hex() == encoded
    assert type(pickle.loads(bytes.fromhex(encoded))) is type(value)


@pytest.mark.parametrize('name', ['QueueRetryPolicy', '_ProcessorOutcome', '_WorkerProgress', 'processor_error_code'])
def test_worker_root_aliases_are_the_same_objects(name):
    assert getattr(memory_queue, name) is getattr(queue_worker_state, name)


def test_prefix_keeps_renderer_identity_and_serialization():
    helper = code_navigation_renderer._utf8_prefix
    encoded = (ROOT / 'tests/modules/utf8_text/legacy-pickle.txt').read_text()
    assert helper is utf8_text._utf8_prefix
    assert pickle.dumps(helper).hex() == encoded
    assert pickle.loads(bytes.fromhex(encoded)) is helper
