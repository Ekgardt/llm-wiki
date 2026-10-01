"""A failed native event retains the identity needed to find its durable intent."""
import io
import json
import subprocess
import sys

import capture_diagnostics as diagnostics
import integration_adapter as adapter
import pytest


@pytest.mark.parametrize('source', ['codex', 'claude', 'opencode'])
@pytest.mark.parametrize('event', ['post_tool_use', 'user_prompt'])
def test_failed_native_event_keeps_identity(monkeypatch, tmp_path, source, event):
    seen = []

    def fail_dispatch(_args, envelope):
        seen.append(envelope)
        raise subprocess.TimeoutExpired('capture', 3.5)

    monkeypatch.delenv('CLAUDE_INVOKED_BY', raising=False)
    monkeypatch.setattr(adapter, '_dispatch_cli_event', fail_dispatch)
    monkeypatch.setattr(diagnostics, 'FAILURE_LOG', tmp_path / 'failures.jsonl')
    monkeypatch.setattr(diagnostics, 'update_state', lambda fn, **kw: fn({}))
    payload = {'session_id': 'session-unique', 'sessionID': 'session-unique', 'event_id': 'source-occurrence',
               'prompt': 'private prompt must not enter diagnostics',
               'tool_name': 'Read', 'tool_input': {'file_path': '/tmp/example'}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))

    assert adapter.main(['--source', source, '--event', event]) == 0

    record = json.loads(diagnostics.FAILURE_LOG.read_text())
    assert record['event_id'] == seen[0].event_id
    assert record['session'] == seen[0].session[:8]
    assert record['outcome'] == 'lost'
    assert 'private prompt' not in diagnostics.FAILURE_LOG.read_text()
