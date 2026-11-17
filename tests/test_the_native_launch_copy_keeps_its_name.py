"""The verified native launch copy of a server keeps its name inside the owner root.

Split from the transport tests of finding K-B17 of the 2026-09-17 audit.
Research: docs/research/2026-09-18-sess-a-lapsed-deadline-is-not-a-torn-stream.md
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from lsp_process import GenerationLaunch
from pyright_session import LaunchServerGuard

from tests.slow_machine import SHORT_TIMEOUT


@pytest.mark.skipif(
    os.name != "posix", reason="the verified launch copy is the POSIX mechanism"
)
def test_the_native_launch_copy_keeps_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no copy to name: `__enter__` verifies the source in place.

    `LaunchServerGuard.__enter__` returns a `GenerationLaunch` only under
    `os.name == "posix"`; elsewhere it returns the guard, which is what
    `lsp_process` expects of it. Same mark as the guard's neighbours in
    `tests/test_pyright_session.py`.
    """
    import hashlib

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    server = tmp_path / "server.bin"
    server.write_bytes(b"native server\n")
    guard = LaunchServerGuard(
        server,
        hashlib.sha256(server.read_bytes()).hexdigest(),
        command=(str(server),),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
        native=True,
    )
    with guard as launch:
        assert isinstance(launch, GenerationLaunch)
        copy = Path(launch.command[0])
        kept = (copy.parent == owner, copy.exists())
    assert (kept, copy.exists(), copy.read_bytes()) == (
        (True, True),
        True,
        b"native server\n",
    )
