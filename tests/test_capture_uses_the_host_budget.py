"""Real CLI capture survives a lock held beyond the retired five-second cutoff.

These are command/SQLite qualifications, not genuine host lifecycle events.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from breadcrumb_storage import load_bundle
from installed_memory_repair import repair_installed_vault

ROOT = Path(__file__).resolve().parents[1]
OLD_CAPTURE_SECONDS = 5
# The documented host defaults, checked 2026-10-02. No product deadline added.
CASES = [('codex', 'UserPromptSubmit', 600), ('codex', 'PostToolUse', 600),
         ('claude-code', 'UserPromptSubmit', 30)]


def _handler(host, event):
    path = {'codex': 'integrations/codex/hooks.json',
            'claude-code': 'integrations/claude-code/settings.json'}[host]
    groups = json.loads((ROOT / path).read_text())['hooks'][event]
    handlers = [handler for group in groups for handler in group['hooks']]
    return next(handler for handler in handlers if 'integration_adapter.py' in handler['command'])


def _vault(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    shutil.copytree(ROOT / 'scripts', vault / 'scripts',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copyfile(ROOT / name, vault / name)
    (vault / '.venv').symlink_to(Path(sys.prefix), target_is_directory=True)
    (vault / 'knowledge/projects').mkdir(parents=True)
    result = repair_installed_vault(root=vault, state_root=vault,
                                   adopt_ownership_v3=True, confirm_all_agents_stopped=True)
    assert result['overall_status'] == 'ok', result
    return vault


def _environment(vault):
    env = dict(os.environ, LLM_WIKI_ROOT=str(vault), LLM_WIKI_STATE_ROOT=str(vault))
    if env.get('MEMORY_LLM_PROVIDER') == 'fake':
        del env['MEMORY_LLM_PROVIDER']
    return env


def _hold_database(path, ready):
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute('BEGIN EXCLUSIVE')
        ready.set()
        # Fault condition: one second beyond the old host deadline, below admission.
        time.sleep(OLD_CAPTURE_SECONDS + 1)


def _terminate_fixture_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _invoke(handler, default, vault, payload):
    env = _environment(vault)
    argv = shlex.split(string.Template(handler['command']).substitute(env))
    with subprocess.Popen(argv, cwd=vault, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            output, errors = process.communicate(json.dumps(payload),
                                                 timeout=handler.get('timeout', default))
        except subprocess.TimeoutExpired:
            _terminate_fixture_group(process)
            process.communicate()
            return 'host_timeout', None
        assert process.returncode == 0, errors
        assert output == ''
        return 'completed', process.returncode


def _payload(vault, event):
    return {'hook_event_name': event, 'session_id': 'host-budget-qualification',
            'turn_id': 'turn-1', 'tool_use_id': 'tool-1', 'cwd': str(vault),
            'prompt': 'Preserve this request after the legitimate lock releases.',
            'tool_name': 'Bash', 'tool_input': {'command': 'git status --short'},
            'tool_response': {'stdout': 'qualification', 'exit_code': 0}}


def _verify_bundle(vault):
    manifests = list((vault / 'run/capture-intents/ready').rglob('*.json'))
    assert len(manifests) == 1
    bundle = load_bundle(vault, manifests[0].read_bytes(),
                         deadline=time.monotonic() + OLD_CAPTURE_SECONDS)
    assert bundle.content


@pytest.mark.skipif(os.name != 'posix', reason='POSIX controlled fixture process-group cleanup')
@pytest.mark.parametrize('host,event,default', CASES)
def test_capture_finishes_after_a_legitimate_long_lock(tmp_path, host, event, default):
    vault = _vault(tmp_path)
    handler = _handler(host, event)
    ready = threading.Event()
    thread = threading.Thread(target=_hold_database,
                              args=(vault / 'run/markdown-transactions-v3.sqlite3', ready))
    thread.start()
    try:
        assert ready.wait(OLD_CAPTURE_SECONDS + 1)
        outcome, _code = _invoke(handler, default, vault, _payload(vault, event))
        assert outcome == 'completed'
        _verify_bundle(vault)
    finally:
        thread.join()
