"""Actual subprocess responses agree with the recorded synthetic baseline."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'benchmark/fixtures/comparative_adapter_fixture.py'
read_source = context_reader(ROOT, ROOT / 'docs/modules/comparative-adapter-fixture-context.json')
BASELINE = json.loads(read_source(Path(__file__).with_name('baseline.json')))
ADAPTERS = tuple(name for name in BASELINE if name != 'transient-failure')


def _run(*args, request=''):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=request,
        text=True, capture_output=True, check=False,
    )


def _request(attempt):
    return json.dumps({'task_id': 'fixture-task-a', 'seed': 1729, 'attempt': attempt})


@pytest.mark.parametrize('adapter', ADAPTERS)
def test_complete_success_payload(adapter):
    result = _run(adapter, request=_request(2))
    assert result.returncode == 0
    assert not result.stderr
    assert json.loads(result.stdout) == BASELINE[adapter]
    assert result.stdout == json.dumps(BASELINE[adapter], sort_keys=True) + '\n'


def test_retry_has_the_original_failure_and_success_payloads():
    first = _run('graphify-pinned', request=_request(1))
    second = _run('graphify-pinned', request=_request(2))
    assert first.returncode == second.returncode == 0
    assert not first.stderr and not second.stderr
    assert json.loads(first.stdout) == BASELINE['transient-failure']
    assert json.loads(second.stdout) == BASELINE['graphify-pinned']


def test_probe_needs_no_json_request():
    result = _run('probe-model')
    assert result.returncode == 0
    assert result.stdout == 'fixture/model@v1\n'
    assert not result.stderr


@pytest.mark.parametrize('args', [(), ('unknown',), ('grep-read', 'extra')])
def test_invalid_command_is_refused(args):
    result = _run(*args)
    assert result.returncode == 2
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize('payload', ['', 'not-json'])
def test_malformed_request_fails_without_a_success_payload(payload):
    result = _run('grep-read', request=payload)
    assert result.returncode != 0
    assert not result.stdout
    assert 'JSONDecodeError' in result.stderr
