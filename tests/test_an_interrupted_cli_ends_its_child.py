"""Cancellation must finish a real child and preserve the original exception."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest
import sync_memory


@pytest.fixture
def interrupted_child(monkeypatch):
    children = []
    exceptions = []
    interrupted = set()
    real_spawn = subprocess.Popen
    real_communicate = subprocess.Popen.communicate

    def spawn(*args, **kwargs):
        child = real_spawn(*args, **kwargs)
        children.append(child)
        return child

    def communicate(child, *args, **kwargs):
        if child.pid not in interrupted:
            assert child.stdout.readline() == b"ready\n"
            interrupted.add(child.pid)
            raise exceptions[0]
        return real_communicate(child, *args, **kwargs)

    monkeypatch.setattr(sync_memory.subprocess, "Popen", spawn)
    monkeypatch.setattr(real_spawn, "communicate", communicate)
    yield children, exceptions
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


@pytest.mark.skipif(
    os.name != "posix",
    reason="Actual POSIX process group; Windows keeps separate taskkill qualification",
)
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit(7), OSError("communication failed")]
)
def test_an_interrupted_call_ends_the_real_child_and_preserves_its_exception(
    interrupted_child, error
):
    children, exceptions = interrupted_child
    exceptions.append(error)
    command = [sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"]
    with pytest.raises(type(error)) as caught:
        sync_memory._run_process_tree(
            command, capture_output=True, timeout=sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS
        )
    assert caught.value is error
    assert len(children) == 1
    assert children[0].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="Actual POSIX child")
def test_an_unproven_interruption_cleanup_is_named_without_replacing_the_exception(
    interrupted_child, monkeypatch, capsys
):
    children, exceptions = interrupted_child
    error = KeyboardInterrupt()
    exceptions.append(error)
    monkeypatch.setattr(sync_memory, "_kill_process_tree", lambda child: "tree_cleanup_failed")
    command = [sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"]
    with pytest.raises(KeyboardInterrupt) as caught:
        sync_memory._run_process_tree(
            command, capture_output=True, timeout=sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS
        )
    assert caught.value is error
    assert error.cleanup_error == "tree_cleanup_failed"
    assert "cleanup unverified" in capsys.readouterr().err
    assert children[0].poll() is not None
