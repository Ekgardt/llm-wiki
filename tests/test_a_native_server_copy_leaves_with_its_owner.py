"""A native server's verified copy leaves with its owner root (audit 2026-09-26 A-6, C-7).

docs/research/2026-09-26-a-native-server-copy-leaves-with-its-owner.md
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import lsp_process
import pytest
from process_liveness import process_start_identity

from tests.test_a_dead_owner_root_is_swept_when_the_next_server_starts import finished_process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the native launch copy is a POSIX path")

OWNER_NONCE = "a" * 32


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_a_closed_owner_root_takes_its_launch_copy_with_it(tmp_path: Path) -> None:
    owner = lsp_process._OwnerDirectory.open(tmp_path / OWNER_NONCE)
    owner.create(time.monotonic() + 5)
    (owner.owner_root / ".launch-x1.tmp").write_bytes(b"\x7fELF")

    owner.remove_success_scratch()

    assert not owner.owner_root.exists()


def _failure_root(tmp_path: Path, record: dict) -> Path:
    root = tmp_path / "lsp" / ("1" * 32)
    root.mkdir(parents=True)
    (root / "owner.json").write_bytes(_canonical(record))
    (root / "failure.json").write_bytes(_canonical({"code": "process_exited"}))
    (root / ".launch-x1.tmp").write_bytes(b"\x7fELF")
    return root


def _contents(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir()}


def test_a_dead_failure_root_keeps_its_records_and_drops_the_copy(tmp_path: Path) -> None:
    pid, identity = finished_process()
    root = _failure_root(tmp_path, {
        "owner_pid": pid, "owner_start_identity": identity, "state": "process_running",
    })
    expected = _contents(root)
    del expected[".launch-x1.tmp"]

    lsp_process._sweep_dead_owner_roots(tmp_path / "lsp" / ("f" * 32))

    assert _contents(root) == expected


@pytest.mark.skipif(sys.platform != "linux", reason="Linux persisted owners require namespace scope")
@pytest.mark.parametrize("identity_fields", [{}, {"owner_start_identity": "linux:legacy:1"}])
def test_an_unscoped_failure_root_keeps_its_copy(tmp_path: Path, identity_fields: dict) -> None:
    pid, _identity = finished_process()
    root = _failure_root(tmp_path, {"owner_pid": pid, "state": "process_running", **identity_fields})
    expected = _contents(root)

    lsp_process._sweep_dead_owner_roots(tmp_path / "lsp" / ("f" * 32))

    assert _contents(root) == expected


def test_a_live_failure_root_keeps_its_copy(tmp_path: Path) -> None:
    root = _failure_root(tmp_path, {
        "owner_pid": os.getpid(),
        "owner_start_identity": process_start_identity(os.getpid()),
        "state": "process_running",
    })
    expected = _contents(root)

    lsp_process._sweep_dead_owner_roots(tmp_path / "lsp" / ("f" * 32))

    assert _contents(root) == expected
