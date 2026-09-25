"""A prune that died after marking its row is finished: recovery removes, not restores.

See docs/research/2026-09-25-a-finished-prune-is-not-undone.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.test_nothing_half_written_is_left_behind import _committed, _coordinator, _kill_a_prune


def test_images_a_marked_row_disowned_are_removed_on_recovery(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            """UPDATE "transaction" SET artifacts_pruned_at = '2026-09-25T00:00:00Z' WHERE id = ?""",
            (transaction_id,),
        )

    coordinator.prune()

    assert (staged.exists(), (coordinator.transaction_root / transaction_id).exists()) == (False, False)
