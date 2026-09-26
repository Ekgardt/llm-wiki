"""A conflict written today is judged even when older history fills the scan (audit 2026-09-26 A-3).

docs/research/2026-09-26-doctor-reads-the-open-rows-first.md
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import doctor
from markdown_transaction import MarkdownCoordinator

from tests.test_every_store_has_a_bound import _append


def test_the_newest_conflict_is_judged_when_the_scan_is_full(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        _append(coordinator, day)
    with sqlite3.connect(coordinator.database_path) as database:
        newest = database.execute('SELECT id FROM "transaction" ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        database.execute("""UPDATE "transaction" SET state = 'conflicted' WHERE id = ?""", (newest,))
    monkeypatch.setattr(doctor, "MAX_OPERATIONAL_ROWS", 3)

    check = doctor._transaction_check(
        tmp_path / "state", datetime.now(timezone.utc), time.monotonic() + 30, vault_root=tmp_path / "vault"
    )

    assert check["status"] != "ok"
