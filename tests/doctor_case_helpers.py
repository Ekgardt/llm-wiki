"""Helpers the doctor scenario files share: fixture builders, snapshots and the generous budget."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.doctor_helpers import (
    GENEROUS_BUDGET_SECONDS,
    _lsp_timestamp,
)


def give_doctor_a_generous_budget(monkeypatch):
    """Give every doctor run in a test file enough time to reach its findings.

    `run_doctor` bounds itself at five seconds and reports "budget exhausted"
    instead of a finding when it runs out. On a loaded hosted Windows runner a
    single repair pass took 8.5 seconds, so tests asserting on findings were
    reading the clock rather than the behaviour. Tests about the budget itself
    pass their own value and keep it.
    """
    import doctor

    original = doctor.run_doctor

    def run(**kwargs):
        kwargs.setdefault("time_budget_seconds", GENEROUS_BUDGET_SECONDS)
        return original(**kwargs)

    monkeypatch.setattr(doctor, "run_doctor", run)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _junction_or_skip(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions are unavailable on this platform")
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junctions unavailable: {result.stderr!r}")


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def _write_lsp_lease(
    owner: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    manager_pid: int,
    server_pid: int,
    heartbeat_at: datetime,
    expires_at: datetime,
) -> None:
    (owner / "lease.json").write_text(
        json.dumps(
            {
                "expires_at": _lsp_timestamp(expires_at),
                "generation_nonce": generation_nonce,
                "heartbeat_at": _lsp_timestamp(heartbeat_at),
                "manager_pid": manager_pid,
                "owner_nonce": owner_nonce,
                "schema_version": 1,
                "server_pid": server_pid,
                "state": "live",
            }
        ),
        encoding="utf-8",
    )


def _moment_days_ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
