"""Helpers shared by test_compile_call_controller.py and the test files importing them."""

from dataclasses import replace

import compile_progress as progress
import pytest
import queue_core
from reliable_memory import sha256_bytes

from tests.compile_progress_admission_helpers import upgraded as upgraded


def _source_scope(path, content):
    return progress.make_source_scope([{
        'logical_path': path, 'byte_start': 0, 'byte_end': len(content),
        'sha256': sha256_bytes(content)}])


@pytest.fixture
def work(upgraded):
    root, state, _ = upgraded
    queue = queue_core.active_memory_queue(root, state)
    lease = queue.claim('compile-worker')
    source = b'verified input snapshot'
    scope = _source_scope('knowledge/daily/2026-10-20.md', source)
    first = progress.CompileEvent('c' * 64, lease.id, 0, 'd' * 64, 'pending', 'ready',
                                 {'source_scope': scope})
    queue.record_compile_event(lease, first)
    checkpoint = queue.save_compile_artifact(
        lease, replace(first, sequence=1, stage='snapshot_ready'), source,
    )
    return queue, lease, checkpoint
