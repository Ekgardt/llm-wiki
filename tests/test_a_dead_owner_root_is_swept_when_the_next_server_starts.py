"""Starting a server takes down the owner roots a dead one left behind.

A controlled close removes its own `run/lsp/<nonce>/`. A failure or an abrupt
death leaves it, with its records and any sealed `launch-` tree, and nothing
else in the product ever removed it. See
`docs/research/2026-09-17-lsp-the-owner-record-names-the-first-generation-only.md`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from lsp_process import LspProcess

from tests.slow_machine import SHORT_TIMEOUT

OWNER_NONCE = "f" * 32
DEAD_NONCE = "0" * 32
LIVE_NONCE = "1" * 32
# A server that stays up until its stdin ends, so the start it is asked for
# actually succeeds and the sweep it triggers is the production one.
_SERVER = "import sys\nsys.stdin.buffer.read()\n"


def _canonical(record: dict) -> bytes:
    """The encoding the product writes its evidence records in."""
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _owner_record(nonce: str, pid: int) -> dict:
    return {
        "command_basename": "pyright-langserver",
        "generation_nonce": "a" * 32,
        "owner_nonce": nonce,
        "owner_pid": pid,
        "started_at": _stamp(),
        "state": "process_running",
    }


def _plant(parent: Path, nonce: str, pid: int, *, failed: bool = False) -> Path:
    """One owner root as an abrupt death leaves it, sealed launch tree and all."""
    root = parent / nonce
    root.mkdir(parents=True)
    (root / "owner.json").write_bytes(_canonical(_owner_record(nonce, pid)))
    if failed:
        (root / "failure.json").write_bytes(_canonical({"code": "process_exited"}))
    launch = root / "launch-abcdef" / "dist"
    launch.mkdir(parents=True)
    (launch / "server.js").write_bytes(b"console.log(1)\n")
    os.chmod(launch / "server.js", 0o400)
    os.chmod(launch, 0o500)
    os.chmod(launch.parent, 0o500)
    return root


@pytest.fixture()
def dead_pid() -> int:
    """A process identifier this machine has finished with and reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def test_a_dead_neighbour_is_swept_and_a_live_one_and_evidence_are_kept(
    tmp_path: Path, dead_pid: int
) -> None:
    parent = tmp_path / "lsp"
    dead = _plant(parent, DEAD_NONCE, dead_pid)
    kept = _plant(parent, LIVE_NONCE, os.getpid())
    with_evidence = _plant(parent, "2" * 32, dead_pid, failed=True)

    process = LspProcess.start(
        [sys.executable, "-c", _SERVER], cwd=tmp_path, owner_root=parent / OWNER_NONCE
    )
    process.close(time.monotonic() + SHORT_TIMEOUT)

    assert (dead.exists(), kept.exists(), with_evidence.exists()) == (
        False,
        True,
        True,
    )
