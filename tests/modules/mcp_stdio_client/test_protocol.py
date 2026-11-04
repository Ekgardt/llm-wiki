"""Finite protocol examples; these do not certify unresolved IO deadlines."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark import mcp_stdio_client as client

ROOT = Path(__file__).resolve().parents[3]
SERVER = '''
import json,sys
mode=sys.argv[1]
def read():
    return json.loads(sys.stdin.readline())
def send(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)
init=read()
assert init['method']=='initialize' and init['id']==1
assert init['params']['protocolVersion']=='2025-06-18'
send({'jsonrpc':'2.0','id':1,'result':{}})
notification=read()
assert notification['method']=='notifications/initialized' and 'id' not in notification
request=read()
assert request['id']==2
results={
 'answered':{'result':{'content':[{'type':'text','text':'Привет'},{'type':'image','data':'ignored'},{'type':'text','text':'second'}]}},
 'tool_error':{'result':{'isError':True,'content':[{'type':'text','text':'refused'}]}},
 'rpc_error':{'error':{'code':-32000,'message':'failed'}},
 'list':{'result':{'tools':[{'name':'echo','description':'local tool'}]}},
}
expected={'list':'tools/list'}.get(mode,'tools/call')
assert request['method']==expected
send({'jsonrpc':'2.0','id':2,**results[mode]})
assert sys.stdin.read()==''
'''


def _cli(mode):
    command = [sys.executable, '-u', '-c', SERVER, mode]
    options = ['--tool', 'echo', '--arguments', '{"value":"hello"}']
    if mode == 'list':
        options = ['--list']
    return subprocess.run(
        [sys.executable, str(ROOT / 'benchmark/mcp_stdio_client.py'),
         '--command', json.dumps(command), *options],
        capture_output=True, text=True, encoding='utf-8', timeout=30, check=True,
    )


@pytest.mark.parametrize('mode,status,text', [
    ('answered', 'answered', 'Привет\nsecond'),
    ('tool_error', 'tool_error', 'refused'),
    ('rpc_error', 'error', "error: {'code': -32000, 'message': 'failed'}"),
])
def test_real_finite_server_handshake_call_and_output(mode, status, text):
    outcome = json.loads(_cli(mode).stdout)
    assert outcome['status'] == status
    assert outcome['text'] == text
    assert isinstance(outcome['seconds'], (int, float)) and outcome['seconds'] >= 0


def test_real_finite_server_lists_tools():
    assert _cli('list').stdout.strip() == 'echo: local tool'


def test_request_and_notification_are_independent_json_lines():
    request = client._request(7, 'tools/call', {'name': 'λ\nquoted'})
    assert request.count(b'\n') == 1 and request.endswith(b'\n')
    assert json.loads(request) == {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'λ\nquoted'}}
    assert json.loads(client._notification('ready')) == {'jsonrpc': '2.0', 'method': 'ready', 'params': {}}


def test_response_correlation_skips_unrelated_and_non_object_lines():
    stream = io.BytesIO(b'banner\n[]\nnull\n{"id":1,"result":{}}\n{"method":"note"}\n{"id":2,"result":{"ok":true}}\n')
    assert client._reply_to(stream, 2) == {'id': 2, 'result': {'ok': True}}


def test_eof_is_a_failure():
    with pytest.raises(client.McpCallFailed, match='closed the connection'):
        client._reply_to(io.BytesIO(b'{}\n'), 2)


@pytest.mark.parametrize('message', [{}, {'result': []}, {'result': None}])
def test_non_object_result_is_rejected(message):
    with pytest.raises(client.McpCallFailed, match='no result object'):
        client._require_result(message)


def test_remote_error_text_is_bounded():
    with pytest.raises(client.McpCallFailed) as failure:
        client._require_result({'error': 'x' * 1000})
    assert str(failure.value) == 'x' * 400


def test_text_blocks_and_non_content_fallback():
    assert client._text_of({'content': [None, {'type': 'text', 'text': ''}, {'type': 'text', 'text': 4}]}) == '4'
    assert json.loads(client._text_of({'value': '中文'})) == {'value': '中文'}


def test_decoding_keeps_existing_replacement_behavior():
    assert client._decoded(b'{"text":"\xff"}') == {'text': '\ufffd'}
