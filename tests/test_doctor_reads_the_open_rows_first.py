"""A conflict is judged wherever it lies in the history (audit 2026-09-26 A-3).

The scan used to be capped and ordered open-rows-first so that today's conflict
was not left behind the cap (docs/research/2026-09-26-doctor-reads-the-open-rows-first.md).
It now reads every row (docs/research/2026-09-27-doctor-reads-every-transaction.md),
so both the newest and the oldest conflict are judged.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from llm_wiki import doctor_transactions
from markdown_transaction import MarkdownCoordinator

from tests.every_store_has_a_bound_helpers import _append


@pytest.mark.parametrize("order", ["DESC", "ASC"])
def test_a_conflict_is_judged_wherever_it_lies(tmp_path: Path, order: str) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        _append(coordinator, day)
    with sqlite3.connect(coordinator.database_path) as database:
        chosen = database.execute(f'SELECT id FROM "transaction" ORDER BY rowid {order} LIMIT 1').fetchone()[0]
        database.execute("""UPDATE "transaction" SET state = 'conflicted' WHERE id = ?""", (chosen,))

    check = doctor_transactions.transaction_check(
        tmp_path / "state", datetime.now(UTC), time.monotonic() + 30, vault_root=tmp_path / "vault"
    )

    assert check["status"] != "ok"
