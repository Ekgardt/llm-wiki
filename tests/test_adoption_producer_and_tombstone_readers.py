"""An adopted vault survives an ordinary producer update, and readers follow tombstones.

Two defects sat between the approved Reliability V3 cutover and a vault that
keeps working afterwards.

The first: the adoption record freezes `sha256(scripts/integration_adapter.py)`
and `_require_adoption_sources` re-checked it on *every* validation. Because
`require_reliability_v3_adopted` guards capture, the queue and every Markdown
transaction, the first edit of that file after adoption would have disabled the
whole memory write path -- including the unattended nightly fast-forward the
owner approved on 2026-08-23.

The second: doctor read the legacy database paths unconditionally. Adoption
turns those into JSON tombstones, so a healthy adopted vault reported
`queue_state_unreadable` and `transaction_state_unreadable`, and both codes also
block deleting `run/`.

The fixture here adopts through the real entry point rather than a hand-built
record, so the tombstones and the adoption record are the ones the cutover
actually writes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import doctor
import pytest
from installed_memory_repair import (
    ReliabilityV3ValidationError,
    adopted_database_path,
    repair_installed_vault,
    require_reliability_v3_adopted,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _adopted(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    for relative in (
        "knowledge/daily",
        "knowledge/notes",
        "knowledge/projects",
        "knowledge/inbox",
        "knowledge/feedback",
        "scripts",
    ):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_text("# Index\n", encoding="utf-8")
    (root / "knowledge/log.md").write_text("# Log\n", encoding="utf-8")
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    state_root.mkdir(parents=True)
    repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    return root, state_root


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_a_changed_producer_does_not_invalidate_an_adopted_vault(tmp_path):
    root, state_root = _adopted(tmp_path)
    before = require_reliability_v3_adopted(root=root, state_root=state_root)

    adapter = root / "scripts" / "integration_adapter.py"
    adapter.write_bytes(adapter.read_bytes() + b"\n# a later, ordinary update\n")

    assert require_reliability_v3_adopted(root=root, state_root=state_root) == before


def test_a_missing_producer_still_fails_closed(tmp_path):
    root, state_root = _adopted(tmp_path)
    (root / "scripts" / "integration_adapter.py").unlink()

    with pytest.raises(ReliabilityV3ValidationError):
        require_reliability_v3_adopted(root=root, state_root=state_root)


def test_adoption_leaves_a_tombstone_where_the_legacy_queue_was(tmp_path):
    _root, state_root = _adopted(tmp_path)

    document = json.loads((state_root / "run" / "queue.sqlite3").read_bytes())

    assert document["schema_version"] == "operational-db-tombstone/v1"


def test_the_resolver_follows_a_tombstone_to_the_adopted_database(tmp_path):
    _root, state_root = _adopted(tmp_path)

    resolved = adopted_database_path(database_name="queue", state_root=state_root)

    assert resolved.name == "queue-v3.sqlite3"


def test_the_resolver_keeps_the_legacy_path_when_there_is_no_tombstone(tmp_path):
    resolved = adopted_database_path(database_name="coordinator", state_root=tmp_path)

    assert resolved == tmp_path / "run" / "markdown-transactions.sqlite3"


def test_doctor_does_not_call_an_adopted_queue_unreadable(tmp_path):
    _root, state_root = _adopted(tmp_path)

    result = doctor._queue_check(state_root, _now(), time.monotonic() + 60)

    assert "queue_state_unreadable" not in json.dumps(result)


def test_doctor_does_not_call_adopted_transactions_unreadable(tmp_path):
    _root, state_root = _adopted(tmp_path)

    result = doctor._transaction_check(state_root, _now())

    assert "transaction_state_unreadable" not in json.dumps(result)
