"""The immutable policy is usable without importing its legacy facade."""
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path

import pytest
from llm_wiki.reliability_defaults import DEFAULTS, ReliableMemoryDefaults

ROOT = Path(__file__).resolve().parents[3]
BASELINE = json.loads((Path(__file__).parent / 'fixtures/legacy-pickle.json').read_text())


def test_values_and_field_order_match_the_original_contract():
    assert asdict(DEFAULTS) == BASELINE['fields']
    assert [field.name for field in fields(ReliableMemoryDefaults)] == list(BASELINE['fields'])
    assert ReliableMemoryDefaults() == DEFAULTS


def test_defaults_remain_frozen():
    with pytest.raises(FrozenInstanceError):
        DEFAULTS.queue_busy_ms = 1


def test_class_keeps_historical_pickle_name():
    assert ReliableMemoryDefaults.__module__ == 'reliable_memory'


def test_import_and_construction_do_not_load_runtime():
    code = '''
import builtins, sys
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'reliable_memory', 'memory_queue', 'memory_state'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from llm_wiki.reliability_defaults import DEFAULTS, ReliableMemoryDefaults
assert DEFAULTS == ReliableMemoryDefaults()
assert not {'reliable_memory', 'memory_queue', 'memory_state'}.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, text=True,
                            capture_output=True, check=False,
                            env={**os.environ, 'PYTHONPATH': str(ROOT / 'scripts')})
    assert result.returncode == 0, result.stderr
