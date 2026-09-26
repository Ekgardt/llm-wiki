"""A long transaction ledger stays readable to the runtime check (audit 2026-09-26 A-11).

docs/research/2026-09-26-a-backup-takes-what-any-installed-vault-holds.md
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import installed_memory_repair

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3

PRUNED_ROWS = 10_050


def _ledger(state_root: Path, now: datetime) -> None:
    old = (now - timedelta(days=31)).isoformat()
    rows = [(f"old-{index}", f"op-{index}", "1" * 64, "committed", "{}", "2" * 64, old, old, old) for index in range(PRUNED_ROWS)]
    rows.append(("recent", "op-recent", "1" * 64, "committed", "{}", "2" * 64, now.isoformat(), now.isoformat(), None))
    with contextlib.closing(sqlite3.connect(state_root / "run/markdown-transactions-v3.sqlite3")) as database:
        database.executemany(
            'INSERT INTO "transaction"(id,operation_id,request_hash,state,preconditions_json,plan_hash,'
            "created_at,updated_at,artifacts_pruned_at) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        database.commit()
    for name in ("recent", "old-7"):
        (state_root / "run/transactions" / name).mkdir(parents=True, exist_ok=True)


def test_a_ledger_past_the_row_bound_is_read_by_what_it_holds(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    now = datetime.now(timezone.utc)
    _ledger(state_root, now)

    blockers = installed_memory_repair.validate_coordinator_v3_runtime(
        state_root=state_root, now=now, deadline=time.monotonic() + SHORT_TIMEOUT, excluded_owner=None
    )

    assert blockers == ["transaction_artifact_retained", "transaction_undo_retained"]
