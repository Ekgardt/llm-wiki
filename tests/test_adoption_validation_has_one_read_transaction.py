"""One validation cannot release its read lock between its invariant checks."""
import contextlib
import json
import sqlite3

import pytest
from llm_wiki import adoption_admission
from markdown_transaction import is_transient_writer_contention

from tests.adopted_capture_vault import adopted_capture_vault


def _exclusive_write_available(writer):
    try:
        writer.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        assert is_transient_writer_contention(error)
        return False
    return True


def _database_record(state_root, name):
    adoption = json.loads((state_root / "run/reliability-v3-adopted.json").read_text(encoding='utf-8'))
    return next(record for record in adoption["databases"] if record["database"] == name)


@pytest.mark.parametrize("name", ["queue", "coordinator"])
def test_a_writer_cannot_interrupt_the_remaining_adoption_checks(monkeypatch, tmp_path, name):
    state_root, _ = adopted_capture_vault(tmp_path, monkeypatch)
    path = state_root / f"run/{'queue' if name == 'queue' else 'markdown-transactions'}-v3.sqlite3"
    record = _database_record(state_root, name)
    original = adoption_admission.database_schema_complete
    attempts = []
    with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as writer:
        def _schema_then_contender(database_name, database):
            complete = original(database_name, database)
            attempts.append(_exclusive_write_available(writer))
            return complete

        monkeypatch.setattr(adoption_admission, "database_schema_complete", _schema_then_contender)
        try:
            adoption_admission.validate_active_database_reference(
                record, database_name=name, path=path, state_root=state_root
            )
        finally:
            writer.rollback()
        assert attempts == [False]
        assert _exclusive_write_available(writer)
        writer.rollback()
