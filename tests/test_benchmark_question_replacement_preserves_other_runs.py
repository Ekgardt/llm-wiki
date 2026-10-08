"""Updating one staged question must preserve another run's linked input."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'benchmark'))
import run_consolidation  # noqa: E402
import run_longmemeval  # noqa: E402


def _completed(*args, **kwargs):
    return SimpleNamespace(returncode=1, stderr='worker did not produce a result')


@pytest.mark.parametrize('runner', [run_longmemeval, run_consolidation])
def test_replacing_a_question_preserves_another_runs_input(tmp_path, monkeypatch, runner):
    staging = tmp_path / 'current'
    staging.mkdir()
    historical = tmp_path / 'historical.question.json'
    original = b'{"question_id":"example","answer":"old evidence"}'
    historical.write_bytes(original)
    current = staging / 'example.question.json'
    os.link(historical, current)
    stale = staging / 'example.result.json'
    stale.write_text('{"status":"ok","hypothesis":"stale result"}')
    monkeypatch.setattr(runner.subprocess, 'run', _completed)
    question = dict(question_id='example', question_type='multi-session', answer='new evidence')
    arguments = SimpleNamespace(workdir=None, keep_vaults=False, provider='fake',
                                provider_timeout=1, concurrency=1)
    result = runner._run_worker(question, staging, arguments)
    assert historical.read_bytes() == original
    assert json.loads(current.read_bytes()) == question
    assert not os.path.samefile(historical, current)
    assert not stale.exists()
    assert result.get('error_kind', result.get('baseline', {}).get('error_kind')) == 'harness_failure'
