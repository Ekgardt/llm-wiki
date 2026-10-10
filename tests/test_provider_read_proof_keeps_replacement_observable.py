"""Sharing allows an editor race; complete held/reopened identity still rejects it."""
import ctypes
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import generation_catalog
import install_control as install
import pytest


@pytest.fixture
def windows_api_on_posix(monkeypatch):
    calls = []
    original_replace = Path.replace

    def create_file(path, access, sharing, security, disposition, flags, template):
        calls.append((access, sharing, security, disposition, flags, template))
        return os.open(Path(path.removeprefix('\\\\?\\')), os.O_RDONLY)

    def replace(source, target):
        if not calls or not calls[-1][1] & 4:
            raise PermissionError('held read handle denies delete sharing')
        return original_replace(source, target)

    platform = SimpleNamespace(name='nt', O_RDONLY=os.O_RDONLY, O_BINARY=0,
                               set_inheritable=os.set_inheritable, close=os.close)
    monkeypatch.setattr(generation_catalog, 'os', platform)
    monkeypatch.setattr(generation_catalog, 'ctypes', ctypes, raising=False)
    monkeypatch.setattr(generation_catalog, '_create_file', create_file, raising=False)
    monkeypatch.setattr(generation_catalog, 'msvcrt', SimpleNamespace(
        open_osfhandle=lambda handle, flags: handle), raising=False)
    monkeypatch.setattr(generation_catalog, '_close_handle', os.close, raising=False)
    monkeypatch.setattr(Path, 'replace', replace)
    return calls


@pytest.mark.skipif(os.name == 'nt', reason='POSIX-hosted Windows API model; native replacement guards run separately')
def test_held_read_allows_replacement_without_losing_old_bytes(tmp_path, windows_api_on_posix):
    path = tmp_path / 'proof.json'
    path.write_bytes(b'original\r\n\x00\xce\xa9')
    replacement = tmp_path / 'replacement.json'
    replacement.write_bytes(b'changed')
    with ExitStack() as stack:
        handle = install._provider_open(stack, path)
        descriptor = handle.fileno()
        replacement.replace(path)
        assert handle.read() == b'original\r\n\x00\xce\xa9'
        assert path.read_bytes() == b'changed'
        assert not os.get_inheritable(descriptor)
        with pytest.raises(OSError):
            os.write(descriptor, b'forbidden')
    assert windows_api_on_posix == [(0x80000000, 7, None, 3, 0x00200000, None)]
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_binary_wrapper_failure_closes_the_transferred_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'proof.json'
    path.write_bytes(b'original')
    opened = []
    original_open = os.open

    def tracked_open(path, flags):
        descriptor = original_open(path, flags)
        opened.append(descriptor)
        return descriptor

    def failed_wrapper(descriptor, mode):
        assert mode == 'rb'
        raise OSError('binary wrapper failed')

    proxy = SimpleNamespace(O_RDONLY=os.O_RDONLY, O_BINARY=getattr(os, 'O_BINARY', 0),
                            O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0), open=tracked_open,
                            fdopen=failed_wrapper, close=os.close)
    monkeypatch.setattr(install, 'os', proxy)
    monkeypatch.setattr(generation_catalog, '_open_read_descriptor',
                        lambda path: tracked_open(path, os.O_RDONLY))
    try:
        with ExitStack() as stack, pytest.raises(OSError, match='binary wrapper failed'):
            install._provider_open(stack, path)
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        _close_if_open(opened[0])


def _close_if_open(descriptor):
    try:
        os.close(descriptor)
    except OSError:
        pass
