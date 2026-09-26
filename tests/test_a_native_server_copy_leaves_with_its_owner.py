"""A native server's verified copy leaves with its owner root (audit 2026-09-26 A-6, C-7).

docs/research/2026-09-26-a-native-server-copy-leaves-with-its-owner.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import lsp_process
import pytest

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


def test_a_dead_failure_root_keeps_its_records_and_drops_the_copy(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    root = tmp_path / "lsp" / ("1" * 32)
    root.mkdir(parents=True)
    (root / "owner.json").write_bytes(_canonical({"owner_pid": child.pid, "state": "process_running"}))
    (root / "failure.json").write_bytes(_canonical({"code": "process_exited"}))
    (root / ".launch-x1.tmp").write_bytes(b"\x7fELF")

    lsp_process._sweep_dead_owner_roots(tmp_path / "lsp" / ("f" * 32))

    assert sorted(path.name for path in root.iterdir()) == ["failure.json", "owner.json"]
