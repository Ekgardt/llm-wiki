"""The data contract imports alone and preserves immutable record semantics."""
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from llm_wiki import queue_contracts as contracts  # noqa: E402

NAMES = json.loads((Path(__file__).parent / 'fixtures/legacy-pickle.json').read_text(encoding='utf-8'))['names']


@pytest.mark.parametrize('name', NAMES)
def test_public_serialization_name_is_stable(name):
    kind = getattr(contracts, name)
    assert kind.__module__ == 'memory_queue'
    assert kind.__qualname__ == name
    assert name in contracts.__all__


@pytest.mark.parametrize('name', NAMES[5:])
def test_records_remain_frozen(name):
    kind = getattr(contracts, name)
    values = {field.name: None for field in dataclasses.fields(kind)}
    record = kind(**values)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(record, next(iter(values)), 'changed')


def test_defaults_and_error_contract():
    assert contracts.QueueFailure('retry') == contracts.QueueFailure('retry', False, None, None)
    assert contracts.WorkerSummary(0, 0, 0, 0, 0).remaining_eligible == 0
    assert contracts.PurgeReceipt(0, ()).retained == ()
    error = contracts.CompileCompletionError('incomplete', 'detail')
    assert isinstance(error, contracts.QueueOperationError)
    assert str(error) == 'incomplete: detail'
    assert error.__reduce__() == (contracts.CompileCompletionError, ('incomplete', 'detail'))
    assert str(contracts.QueueOperationError('busy')) == 'busy'


def test_import_and_construction_need_no_executor_or_side_effects():
    script = r'''
import sys
import importlib.abc
import os
sys.path.insert(0, sys.argv[1])
class StdlibOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split('.')[0]
        allowed = root in sys.stdlib_module_names or fullname in ('llm_wiki', 'llm_wiki.queue_contracts')
        if not allowed:
            raise ModuleNotFoundError(fullname)

def audit(event, args):
    assert not event.startswith(('socket.', 'subprocess.', 'sqlite3.', 'os.mkdir', 'os.remove', 'os.rename')), event
    if event == 'open':
        assert not (args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)), args

sys.meta_path.insert(0, StdlibOnly())
sys.addaudithook(audit)
from llm_wiki.queue_contracts import QueueFailure, DeferredResult, QueueOperationError
assert QueueFailure('retry').permanent is False
assert DeferredResult(b'answer').data == b'answer'
assert QueueOperationError('busy').code == 'busy'
assert 'memory_queue' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(ROOT / 'scripts')],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
