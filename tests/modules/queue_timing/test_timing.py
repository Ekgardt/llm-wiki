"""Value-level contracts do not need a queue or database."""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from llm_wiki import queue_timing as timing

NOW = datetime(2026, 11, 2, 12, 34, 56, 123456, tzinfo=timezone.utc)


@pytest.mark.parametrize('offset', [-7, 0, 9])
def test_aware_time_round_trip_keeps_instant_and_microseconds(offset):
    value = NOW.astimezone(timezone(timedelta(hours=offset)))
    assert timing._as_utc(value) == NOW
    assert timing._timestamp(value) == '2026-11-02T12:34:56.123456+00:00'
    assert timing._parse_timestamp(value.isoformat()) == NOW


def test_naive_timestamp_is_utc_and_none_remains_absent():
    assert timing._as_utc(NOW.replace(tzinfo=None)) == NOW
    assert timing._parse_timestamp('2026-11-02T12:34:56.123456') == NOW
    assert timing._parse_timestamp(None) is None
    with pytest.raises(ValueError):
        timing._parse_timestamp('not a timestamp')


@pytest.mark.parametrize(('value', 'expected'), [
    (None, None), (True, None), (False, None), (-1, None), (float('nan'), None),
    (float('inf'), None), (float('-inf'), None), (0, 0), (1.5, 1.5),
    (604801, 604800), (' 60 ', 60), ('²', None), ('-1', None), ('1.5', None),
    ('bad', None), (10**400, 604800), (-10**400, None),
    (NOW + timedelta(hours=1), 3600), (NOW - timedelta(hours=1), 0),
    (NOW + timedelta(days=8), 604800),
    (NOW.replace(tzinfo=None) + timedelta(seconds=1), 1),
    ('Tue, 03 Nov 2026 12:34:56 GMT', 86399.876544),
])
def test_retry_after_contract(value, expected):
    assert timing._retry_after_seconds(value, NOW) == expected


@pytest.mark.parametrize(('attempts', 'ceiling'), [(0, 30), (1, 30), (2, 60), (8, 3600), (100, 3600)])
@pytest.mark.parametrize('edge', [0, 1])
def test_full_jitter_keeps_exact_bounds(monkeypatch, attempts, ceiling, edge):
    calls = []

    def uniform(low, high):
        calls.append((low, high))
        return edge * high

    monkeypatch.setattr(timing, '_RETRY_RANDOM', SimpleNamespace(uniform=uniform))
    result = timing._retry_available_at('ready', attempts, NOW)
    assert calls == [(0, ceiling)]
    assert result == NOW + timedelta(seconds=edge * ceiling)


@pytest.mark.parametrize(('stated', 'expected'), [(5, 20), (90, 90), ('²', 20)])
def test_retry_after_is_a_floor_under_jitter(monkeypatch, stated, expected):
    monkeypatch.setattr(timing, '_RETRY_RANDOM', SimpleNamespace(uniform=lambda *_: 20))
    assert timing._retry_available_at('ready', 1, NOW, stated) == NOW + timedelta(seconds=expected)


@pytest.mark.parametrize('state', ['blocked', 'dead', 'cancelled', 'succeeded'])
def test_non_ready_state_returns_same_now_without_randomness(monkeypatch, state):
    monkeypatch.setattr(timing, '_RETRY_RANDOM', None)
    assert timing._retry_available_at(state, 1, NOW, 3600) is NOW


def test_import_and_calculation_do_not_load_executor_or_storage():
    root = Path(__file__).resolve().parents[3]
    code = '''
import builtins, sys
from datetime import datetime, timezone
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'memory_queue', 'reliable_memory', 'memory_state', 'markdown_transaction'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from llm_wiki.queue_timing import _retry_after_seconds, _timestamp
now = datetime(2026, 11, 2, tzinfo=timezone.utc)
assert _retry_after_seconds('60', now) == 60
assert _timestamp(now) == '2026-11-02T00:00:00.000000+00:00'
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, text=True,
                            capture_output=True, check=False, timeout=30,
                            env={**os.environ, 'PYTHONPATH': str(root / 'scripts')})
    assert result.returncode == 0, result.stderr
