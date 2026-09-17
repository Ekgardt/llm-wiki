"""One `node --version` answer stands for the executable it was taken from.

Finding K-B15 of the 2026-09-17 audit: every `manager.get()` -- so every
navigation query -- spawned Node again, and a transient spawn failure
unqualified a checkout that had a healthy session.
Research: docs/research/2026-09-17-sess-a-session-that-failed-to-close-or-start-is-tried-again.md
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pyright_profile
import pytest
from pyright_profile import PyrightCandidates, discover_pyright
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_pyright_fixture
from tests.test_pyright_profile import _FakeNodeProcess, _install_fake_node


@pytest.fixture(autouse=True)
def _empty_probe_cache() -> None:
    pyright_profile._NODE_PROBE_CACHE.clear()


def _node_path(tmp_path: Path) -> Path:
    return tmp_path / ("node.exe" if os.name == "nt" else "node")


def _discover(scope, state_root: Path, server: Path):
    return discover_pyright(
        scope,
        state_root=state_root,
        candidates=PyrightCandidates((server,), (), ()),
    )


def _fake_node(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: bytes):
    return _install_fake_node(monkeypatch, tmp_path, lambda: _FakeNodeProcess(output))


def test_the_same_node_is_probed_once_for_many_discoveries(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    server = create_pyright_fixture(repository)
    processes, _trees = _fake_node(monkeypatch, tmp_path, b"v22.23.1\n")

    results = [_discover(scope, state_root, server) for _ in range(3)]

    assert (
        {result.status for result in results},
        {result.node_version for result in results},
        len(processes),
    ) == ({"qualified"}, {"v22.23.1"}, 1)


def test_a_rewritten_node_is_probed_again(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    server = create_pyright_fixture(repository)
    processes, _trees = _fake_node(monkeypatch, tmp_path, b"v22.23.1\n")

    first = _discover(scope, state_root, server)
    _node_path(tmp_path).write_bytes(b"a different synthetic node executable\n")
    second = _discover(scope, state_root, server)

    assert (first.status, second.status, len(processes)) == (
        "qualified",
        "qualified",
        2,
    )


def test_a_failed_probe_is_never_kept(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    server = create_pyright_fixture(repository)
    processes, _trees = _fake_node(monkeypatch, tmp_path, b"not a version\n")

    results = [_discover(scope, state_root, server) for _ in range(2)]

    assert (
        {result.status for result in results},
        len(processes),
    ) == ({"degraded"}, 2)


def test_a_kept_probe_expires(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    tmp_path: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    server = create_pyright_fixture(repository)
    processes, _trees = _fake_node(monkeypatch, tmp_path, b"v22.23.1\n")

    _discover(scope, state_root, server)
    pyright_profile._NODE_PROBE_CACHE.update(
        {
            key: (time.monotonic() - 1.0, value)
            for key, (_expiry, value) in pyright_profile._NODE_PROBE_CACHE.items()
        }
    )
    second = _discover(scope, state_root, server)

    assert (second.status, len(processes)) == ("qualified", 2)
