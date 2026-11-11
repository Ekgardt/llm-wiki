"""A real spawn exchanges bytes while both sides remain independent of executors."""
import os
import subprocess
import sys
from pathlib import Path

import llm_wiki.queue_child_runtime as child
import pytest
from llm_wiki.queue_contracts import DeferredResult

from tests.slow_machine import SHORT_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = {'memory_queue', 'memory_state', 'capture_diagnostics', 'markdown_transaction'}


def _result(task):
    return {'boolean': True, 'deferred': DeferredResult(b'answer')}[task['kind']]


def _entry(sender, processor, task):
    assert not FORBIDDEN.intersection(sys.modules)
    if os.name != 'nt':
        os.setsid()
    try:
        child.send_processor_frame(sender, child.encode_processor_outcome(processor(task)))
    finally:
        sender.close()


def isolated_roundtrip(kind):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split('.')[0] in FORBIDDEN:
            raise AssertionError('executor import: ' + name)
        return original(name, *args, **kwargs)

    builtins.__import__ = guarded
    result = child.run_processor_child(_result, {'kind': kind}, SHORT_TIMEOUT, child_entry=_entry)
    assert result == _result({'kind': kind})
    assert not FORBIDDEN.intersection(sys.modules)


@pytest.mark.parametrize('kind', ['boolean', 'deferred'])
def test_spawn_roundtrip_without_executor(kind):
    code = ('from tests.modules.queue_child_runtime.test_spawn import isolated_roundtrip; '
            f'isolated_roundtrip({kind!r})')
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, text=True, capture_output=True,
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'scripts')}, check=False,
        timeout=SHORT_TIMEOUT * 2,
    )
    assert result.returncode == 0, result.stderr
