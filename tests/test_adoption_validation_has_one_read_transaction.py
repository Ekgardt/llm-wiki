"""One validation cannot release its read lock between its invariant checks."""
import contextlib
import json
import sqlite3

import installed_memory_repair as repair
import integration_adapter
import pytest
from markdown_transaction import _is_transient_writer_contention

from tests.adopted_capture_vault import adopted_capture_vault


def _exclusive_write_available(writer):
    try:
        writer.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        assert _is_transient_writer_contention(error)
        return False
    return True


def _database_record(state_root, name):
    adoption = json.loads((state_root / "run/reliability-v3-adopted.json").read_text())
    return next(record for record in adoption["databases"] if record["database"] == name)


@pytest.mark.parametrize("name", ["queue", "coordinator"])
def test_a_writer_cannot_interrupt_the_remaining_adoption_checks(monkeypatch, tmp_path, name):
    state_root, _ = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    path = state_root / f"run/{'queue' if name == 'queue' else 'markdown-transactions'}-v3.sqlite3"
    record = _database_record(state_root, name)
    original = repair._database_schema_complete
    attempts = []
    with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as writer:
        def _schema_then_contender(database_name, database):
            complete = original(database_name, database)
            attempts.append(_exclusive_write_available(writer))
            return complete

        monkeypatch.setattr(repair, "_database_schema_complete", _schema_then_contender)
        try:
            repair._validate_active_database_reference(
                record, database_name=name, path=path, state_root=state_root
            )
        finally:
            writer.rollback()
        assert attempts == [False]
        assert _exclusive_write_available(writer)
        writer.rollback()
