"""The sweep of `run/lsp` reaches every dead root and proves death by start identity.

Audit C-39, docs/research/2026-09-25-the-lsp-sweep-reaches-every-dead-root.md.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lsp_process import _sweep_dead_owner_roots  # noqa: E402
from process_liveness import process_start_identity  # noqa: E402

EVIDENCE_ROOTS = 300
DEAD_ROOTS = 50


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _plant(parent: Path, index: int, pid: int, **extra) -> Path:
    root = parent / f"{index:032x}"
    root.mkdir(parents=True)
    record = {"owner_pid": pid, "state": "process_running", **extra}
    (root / "owner.json").write_bytes(_canonical(record))
    return root


@pytest.fixture()
def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _sweep(parent: Path) -> None:
    _sweep_dead_owner_roots(parent / ("f" * 32))


def test_roots_that_kept_evidence_do_not_hide_the_dead_ones(tmp_path, dead_pid):
    parent = tmp_path / "lsp"
    for index in range(EVIDENCE_ROOTS):
        root = _plant(parent, index, dead_pid)
        (root / "failure.json").write_bytes(_canonical({"code": "process_exited"}))
    dead = [_plant(parent, EVIDENCE_ROOTS + index, dead_pid) for index in range(DEAD_ROOTS)]

    _sweep(parent)

    assert sum(root.exists() for root in dead) == 0
    assert len(list(parent.iterdir())) == EVIDENCE_ROOTS


def test_a_pid_now_naming_a_later_process_is_dead(tmp_path):
    parent = tmp_path / "lsp"
    mine = os.getpid()
    reused = _plant(parent, 1, mine, owner_start_identity="0:1")
    live = _plant(parent, 2, mine, owner_start_identity=process_start_identity(mine))
    legacy = _plant(parent, 3, mine)

    _sweep(parent)

    assert (reused.exists(), live.exists(), legacy.exists()) == (False, True, True)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_the_unseal_never_follows_a_link_out_of_the_root(tmp_path, dead_pid):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"kept\n")
    os.chmod(outside, 0o400)
    root = _plant(tmp_path / "lsp", 1, dead_pid)
    (root / "link").symlink_to(outside)

    _sweep(tmp_path / "lsp")

    assert (root.exists(), stat.S_IMODE(outside.stat().st_mode)) == (False, 0o400)
