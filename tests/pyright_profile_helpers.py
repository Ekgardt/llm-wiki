"""Helpers shared by test_pyright_profile.py and the test files importing them."""
from __future__ import annotations

import io
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pyright_profile


class _FakeNodeProcess:
    def __init__(
        self,
        output: bytes,
        *,
        returncode: int = 0,
        times_out: bool = False,
        persistent_timeout: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(output)
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._times_out = times_out
        self._persistent_timeout = persistent_timeout
        self.killed = False
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []
        self.tree: _FakeNodeTree | None = None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.extend([] if timeout is None else [timeout])
        if self._still_times_out():
            raise subprocess.TimeoutExpired(("node", "--version"), timeout)
        self.returncode = -9 if self.killed else self._final_returncode
        return self.returncode

    def _still_times_out(self) -> bool:
        """A persistent timeout outlives the kill; a one-shot one does not."""
        return self._times_out and (self._persistent_timeout or not self.killed)

    def kill(self) -> None:
        self.kill_calls += 1
        self.killed = True


class _FakeNodeTree:
    def __init__(self, process: _FakeNodeProcess) -> None:
        self.process = process
        self.descendants_alive = False
        self.terminate_deadlines: list[float] = []
        self.close_calls = 0
        self.closed = False

    def has_live_descendants(self) -> bool:
        if self.process.returncode is None:
            raise RuntimeError("direct Node probe is still live")
        return self.descendants_alive

    def terminate(self, *, deadline: float) -> None:
        self.terminate_deadlines.append(deadline)
        if self.process.returncode is None:
            self.process.kill()
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        self.descendants_alive = False

    def close(self) -> None:
        self.close_calls += 1
        if self.process.returncode is None or self.descendants_alive:
            raise RuntimeError("Node probe tree is still live")
        self.closed = True


def _install_fake_node(monkeypatch, tmp_path: Path, make_process) -> tuple[list, list]:
    """A synthetic node binary whose every spawned process and tree is recorded."""
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"synthetic node executable\n")
    processes: list = []
    trees: list = []

    def spawn_tree(*args: object, **kwargs: object) -> _FakeNodeTree:
        process = make_process()
        tree = _FakeNodeTree(process)
        process.tree = tree
        processes.append(process)
        trees.append(tree)
        return tree

    monkeypatch.setattr(
        pyright_profile.shutil, "which", lambda *args, **kwargs: str(node)
    )
    monkeypatch.setattr(
        pyright_profile,
        "ProcessTree",
        SimpleNamespace(spawn_with_deadline=spawn_tree),
    )
    return processes, trees
