"""A verified retry resolves its own ancestors, not unrelated dead work."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import doctor
import pytest
from reliable_memory import _harden_runtime_owner_only


def _rows(root, *, state="succeeded", digest_ok=True, same_input=True):
    result = root / "run/queue-results/result.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(b'{"result":"accepted"}')
    _harden_runtime_owner_only(result, 0o600)
    digest = hashlib.sha256(result.read_bytes()).hexdigest()
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE tasks(id, state, updated_at, redrive_of, input_hash, kind, result_reference, result_sha256)")
    moment = "2026-09-01T00:00:00+00:00"
    values = [
        ("parent", "dead", moment, None, "input", "flush", None, None),
        ("middle", "dead", moment, "parent", "input", "flush", None, None),
        ("child", state, moment, "middle", "input" if same_input else "other", "flush", "run/queue-results/result.json", digest if digest_ok else "0" * 64),
        ("unrelated", "dead", moment, None, "input", "flush", None, None),
    ]
    database.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", values)
    return database.execute("SELECT * FROM tasks").fetchall()


def test_verified_success_resolves_the_whole_retry_chain(tmp_path) -> None:
    rows = _rows(tmp_path)
    assert doctor._resolved_queue_ancestors(rows, tmp_path) == {"parent", "middle"}


@pytest.mark.parametrize("options", [{"state": "ready"}, {"digest_ok": False}, {"same_input": False}])
def test_unfinished_changed_or_corrupt_retry_does_not_resolve_history(tmp_path, options) -> None:
    assert doctor._resolved_queue_ancestors(_rows(tmp_path, **options), tmp_path) == set()


def test_only_unresolved_dead_work_contributes_to_backlog(tmp_path) -> None:
    rows = _rows(tmp_path)
    resolved = doctor._resolved_queue_ancestors(rows, tmp_path)
    assert doctor._dead_backlog(rows, datetime(2026, 9, 29, tzinfo=timezone.utc), resolved) == (1, 28)
