"""A live process holding the writer gate past its lease is named (audit 2026-09-27 C-3).

A release the busy database refused is retried for about an hour and then
given up; the row stays and every other writer waits, with nothing said.
docs/research/2026-09-27-a-writer-past-its-lease-is-named.md
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import doctor
from iso_time import utc_text
from markdown_transaction import MarkdownCoordinator

from tests.test_every_store_has_a_bound import _append


def _writer_row(database_path: Path, expires_at: datetime) -> None:
    with sqlite3.connect(database_path) as database:
        columns = [row[1] for row in database.execute("PRAGMA table_info(writer_owners)")]
        values = {
            "gate_name": "global",
            "owner_token": "t" * 32,
            "process_id": os.getpid(),
            "thread_id": 1,
            "acquired_at": utc_text(expires_at - timedelta(hours=2)),
            "heartbeat_at": utc_text(expires_at - timedelta(seconds=30)),
            "expires_at": utc_text(expires_at),
            "fencing_epoch": 1,
        }
        present = [name for name in columns if name in values]
        database.execute(
            f"INSERT INTO writer_owners ({','.join(present)}) VALUES ({','.join('?' * len(present))})",
            [values[name] for name in present],
        )


def _check(tmp_path: Path, expires_at: datetime) -> dict:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    _append(coordinator, "2026-01-01")
    _writer_row(coordinator.database_path, expires_at)
    now = datetime.now(UTC)
    return doctor._transaction_check(tmp_path / "state", now, time.monotonic() + 30, vault_root=tmp_path / "vault")


def test_a_live_writer_past_its_lease_degrades_and_is_named(tmp_path: Path) -> None:
    check = _check(tmp_path, datetime.now(UTC) - timedelta(minutes=70))

    assert (check["status"], check["details"].get("overdue_writers")) == ("degraded", 1)
    assert "past its lease" in check["message"]


def test_a_live_writer_inside_its_lease_is_not_a_finding(tmp_path: Path) -> None:
    check = _check(tmp_path, datetime.now(UTC) + timedelta(seconds=30))

    assert (check["status"], check["details"].get("overdue_writers")) == ("ok", 0)
