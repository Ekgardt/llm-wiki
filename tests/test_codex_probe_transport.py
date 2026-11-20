"""Real local subprocesses exercise the protocol without Codex or a model."""
import os
import sys
import time
from pathlib import Path

import doctor

from tests.slow_machine import CHILD_START_TIMEOUT, LONG_TIMEOUT, PAUSE_TIMEOUT


def _peer_is_gone(pid: int) -> bool:
    """Whether the peer was killed and reaped, asked the way each platform answers.

    `os.kill(pid, 0)` is a POSIX idiom; on Windows it raises `WinError 87` for
    a dead pid (and a `SystemError` on some builds), so the kernel is asked.
    """
    if os.name == "nt":
        from lsp_process_tree import _windows_pid_alive

        return not _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _probe(tmp_path: Path, code: str, seconds: float = 1) -> bytes | object | None:
    script = tmp_path / "peer.py"
    script.write_text(code)
    return doctor._run_codex_probe(
        [sys.executable, str(script)], tmp_path, tmp_path, time.monotonic() + seconds
    )


def test_peer_receives_sequential_handshake_before_stdin_eof(tmp_path):
    code = '''import json,sys
first=json.loads(input())
assert first['method']=='initialize'
print(json.dumps({'id':1,'result':{}}),flush=True)
assert json.loads(input())['method']=='initialized'
request=json.loads(input())
assert request['method']=='hooks/list'
print(json.dumps({'id':2,'result':{'peer':'confirmed'}}),flush=True)
assert sys.stdin.read()==''
'''
    raw = _probe(tmp_path, code)
    assert isinstance(raw, bytes)
    assert doctor._codex_hooks_result(raw) == {"peer": "confirmed"}


def test_hung_peer_is_killed_and_reaped_within_cleanup_budget(tmp_path):
    # The peer outlives the hang bound, so only a kill can end the probe within it.
    code = f'''import os,time
open('peer.pid','w').write(str(os.getpid()))
time.sleep({PAUSE_TIMEOUT})
'''
    started = time.monotonic()
    assert _probe(tmp_path, code, seconds=CHILD_START_TIMEOUT) is doctor._PROBE_INCOMPLETE
    assert time.monotonic() - started < LONG_TIMEOUT
    pid = int((tmp_path / "peer.pid").read_text())
    assert _peer_is_gone(pid)


def test_excessive_peer_output_is_bounded_and_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "MAX_CODEX_HOOK_PROBE_BYTES", 64)
    code = '''import os,time
open('peer.pid','w').write(str(os.getpid()))
print('x'*1024,flush=True)
time.sleep(60)
'''
    assert _probe(tmp_path, code) is doctor._PROBE_INCOMPLETE
    pid = int((tmp_path / "peer.pid").read_text())
    assert _peer_is_gone(pid)


def test_initialize_error_is_not_accepted_as_a_handshake(tmp_path):
    code = '''import json,sys
input()
print(json.dumps({'id':1,'error':{'code':-1,'message':'denied'}}),flush=True)
sys.stdin.read()
'''
    assert _probe(tmp_path, code) is doctor._PROBE_INCOMPLETE
