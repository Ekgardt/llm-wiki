"""The original native token probe retains ABI, errors and resource lifetimes."""
import ctypes
import os
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from llm_wiki import windows_actor as actor


class NativeFunction:
    def __call__(self, *args):
        return 1


def test_native_bindings_keep_pointer_width_and_saved_error_functions():
    advapi = SimpleNamespace(OpenProcessToken=NativeFunction(), GetTokenInformation=NativeFunction(), ConvertSidToStringSidW=NativeFunction())
    kernel = SimpleNamespace(GetCurrentProcess=NativeFunction(), LocalFree=NativeFunction(), CloseHandle=NativeFunction())
    api = actor._windows_token_api(advapi, kernel)
    assert api.get_current_process.restype is wintypes.HANDLE
    assert api.open_process_token.argtypes == (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    assert api.open_process_token.restype is wintypes.BOOL
    assert api.get_token_information.argtypes == (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    assert api.get_token_information.restype is wintypes.BOOL
    assert api.convert_sid.argtypes == (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    assert api.convert_sid.restype is wintypes.BOOL
    assert api.local_free.argtypes == (ctypes.c_void_p,)
    assert api.local_free.restype is ctypes.c_void_p
    assert api.close_handle.argtypes == (wintypes.HANDLE,)
    assert api.close_handle.restype is wintypes.BOOL


def test_open_token_retains_high_handles_and_only_queries_token():
    handle = 1 << 40
    calls = []

    def open_token(process, access, output):
        calls.append((process, access))
        ctypes.cast(output, ctypes.POINTER(wintypes.HANDLE))[0] = handle
        return 1

    api = SimpleNamespace(get_current_process=lambda: handle + 1, open_process_token=open_token)
    assert actor._windows_open_token(api).value == handle
    assert calls == [(handle + 1, 0x0008)]


def native_error(monkeypatch, code):
    error = OSError(code, 'native probe failure')
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: code, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda value: error, raising=False)
    return error


def test_open_failure_propagates_native_error(monkeypatch):
    error = native_error(monkeypatch, 5)
    api = SimpleNamespace(get_current_process=lambda: 123, open_process_token=lambda *args: 0)
    with pytest.raises(OSError) as raised:
        actor._windows_open_token(api)
    assert raised.value is error


def test_token_buffer_uses_reported_size_then_reads(monkeypatch):
    native_error(monkeypatch, 122)
    calls = []

    def read(token, kind, buffer, capacity, required):
        calls.append((token, kind, capacity))
        ctypes.cast(required, ctypes.POINTER(wintypes.DWORD))[0] = 17
        return int(buffer is not None)

    buffer = actor._windows_token_information(SimpleNamespace(get_token_information=read), 1 << 40)
    assert len(buffer) == 17
    assert calls == [(1 << 40, 1, 0), (1 << 40, 1, 17)]


@pytest.mark.parametrize(('code', 'size'), [(5, 17), (122, 0)])
def test_invalid_size_probe_refuses_before_second_call(monkeypatch, code, size):
    error = native_error(monkeypatch, code)
    calls = []

    def read(token, kind, buffer, capacity, required):
        calls.append(capacity)
        ctypes.cast(required, ctypes.POINTER(wintypes.DWORD))[0] = size
        return 0

    with pytest.raises(OSError) as raised:
        actor._windows_token_information(SimpleNamespace(get_token_information=read), 123)
    assert raised.value is error
    assert calls == [0]


def test_failed_second_token_read_propagates(monkeypatch):
    error = native_error(monkeypatch, 122)
    calls = []

    def read(token, kind, buffer, capacity, required):
        calls.append(capacity)
        ctypes.cast(required, ctypes.POINTER(wintypes.DWORD))[0] = 17
        return 0

    with pytest.raises(OSError) as raised:
        actor._windows_token_information(SimpleNamespace(get_token_information=read), 123)
    assert raised.value is error
    assert calls == [0, 17]


def test_sid_text_allocation_is_freed_after_copy(monkeypatch):
    sid = ctypes.c_void_p(1 << 40)
    token_buffer = ctypes.create_string_buffer(ctypes.sizeof(sid))
    ctypes.memmove(token_buffer, ctypes.byref(sid), ctypes.sizeof(sid))
    text_buffer = ctypes.create_unicode_buffer('S-1-5-21')
    freed = []
    monkeypatch.setattr(actor, '_windows_token_information', lambda *args: token_buffer)

    def convert(value, output):
        assert value == sid.value
        ctypes.cast(output, ctypes.POINTER(wintypes.LPWSTR))[0] = ctypes.cast(text_buffer, wintypes.LPWSTR)
        return 1

    api = SimpleNamespace(convert_sid=convert, local_free=lambda p: freed.append(ctypes.cast(p, ctypes.c_void_p).value))
    assert actor._windows_token_sid_text(api, 123) == 'S-1-5-21'
    assert freed == [ctypes.addressof(text_buffer)]


@pytest.mark.parametrize('value', ['S-1-5-21', None, 'malformed'])
def test_actor_closes_token_before_return_or_validation_failure(monkeypatch, value):
    closed = []
    libraries = []
    api = SimpleNamespace(close_handle=closed.append)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kwargs: libraries.append((name, kwargs)), raising=False)
    monkeypatch.setattr(actor, '_windows_token_api', lambda *args: api)
    monkeypatch.setattr(actor, '_windows_open_token', lambda api: 1 << 40)
    monkeypatch.setattr(actor, '_windows_token_sid_text', lambda *args: value)
    if value == 'S-1-5-21':
        assert actor._windows_actor_identity() == 'windows-sid:S-1-5-21'
    else:
        with pytest.raises(OSError):
            actor._windows_actor_identity()
    assert closed == [1 << 40]
    assert libraries == [('advapi32', {'use_last_error': True}), ('kernel32', {'use_last_error': True})]


def test_actor_closes_token_when_sid_query_raises(monkeypatch):
    closed = []
    error = OSError('token read failed')
    api = SimpleNamespace(close_handle=closed.append)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(actor, '_windows_token_api', lambda *args: api)
    monkeypatch.setattr(actor, '_windows_open_token', lambda api: 123)

    def fail(*args):
        raise error

    monkeypatch.setattr(actor, '_windows_token_sid_text', fail)
    with pytest.raises(OSError) as raised:
        actor._windows_actor_identity()
    assert raised.value is error
    assert closed == [123]


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows process token required')
def test_native_process_token_reports_current_sid():
    value = actor._windows_actor_identity()
    assert value.startswith('windows-sid:S-')
    assert len(value.encode('ascii')) <= 256
