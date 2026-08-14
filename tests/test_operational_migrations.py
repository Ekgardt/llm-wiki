from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import markdown_transaction
import memory_queue
import pytest
from reliable_memory import (
    MigrationStatement,
    OperationalDatabaseContract,
    OperationalDatabaseContractError,
    open_operational_db,
    run_resumable_migration,
)

QUEUE_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)
COORDINATOR_APPLICATION_ID = 0x4C575433

COORDINATOR_V3_TABLES = {
    "capture_binding_projections",
    "intent_fence_epochs",
    "intent_fences",
    "maintenance_owner_epochs",
    "maintenance_owners",
    "operation",
    "project_checkpoint_attempts",
    "project_checkpoints",
    "project_leases",
    "transaction",
    "writer_fences",
    "writer_owners",
}

QUEUE_V3_TABLES = {
    "attempt_history",
    "capture_intents",
    "capture_task_link_resolutions",
    "capture_task_link_seals",
    "capture_task_links",
    "corrupt_dispositions",
    "corrupt_export_operations",
    "corrupt_export_pages",
    "corrupt_package_supersession_operations",
    "corrupt_package_supersession_pages",
    "corrupt_package_supersessions",
    "corrupt_purge_operations",
    "corrupt_purge_pages",
    "queue_ownership",
    "semantic_decisions",
    "source_failures",
    "source_fences",
    "task_fence_epochs",
    "task_fences",
    "task_purge_authorizations",
    "task_source_links",
    "tasks",
}

QUEUE_V3_INDEXES = {
    "queue_attempt_history",
    "queue_claim_order",
    "queue_dedupe_identity",
    "queue_redrive_parent",
    "queue_source_tasks",
}


def _table_exists(database: sqlite3.Connection, name: str) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _row_exists(database: sqlite3.Connection, name: str) -> bool:
    return database.execute(
        "SELECT 1 FROM migration_values WHERE name = ?", (name,)
    ).fetchone() is not None


def test_queue_v3_fresh_schema_has_complete_invariant(tmp_path: Path) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    initialized = memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    validated = memory_queue.validate_queue_v3_database(path, state_root=tmp_path)

    assert initialized == {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
    assert validated["application_id"] == 0x4C575133
    assert validated["user_version"] == 3
    assert validated["integrity_check"] == "ok"
    with contextlib.closing(sqlite3.connect(path)) as database:
        objects = database.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {name for kind, name, _sql in objects if kind == "table"}
        indexes = {name for kind, name, _sql in objects if kind == "index"}
        triggers = {name for kind, name, _sql in objects if kind == "trigger"}
        task_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
        task_columns = {
            row[1]: row[2].upper() for row in database.execute("PRAGMA table_info(tasks)")
        }

    assert tables == QUEUE_V3_TABLES
    assert QUEUE_V3_INDEXES <= indexes
    assert {
        "attempt_history_immutable_update",
        "attempt_history_authorized_delete",
        "capture_task_links_immutable_update",
        "capture_task_link_resolutions_immutable_update",
        "capture_task_link_seals_immutable_update",
        "semantic_decisions_immutable_update",
        "semantic_decisions_immutable_delete",
        "queue_lineage_insert",
        "queue_lineage_update_old",
        "queue_lineage_update_new",
        "queue_lineage_delete",
    } <= triggers
    assert task_columns["payload_blob"] == "BLOB"
    assert "length(payload_blob) <= 1048576" in task_sql
    assert "attempts BETWEEN 0 AND 100" in task_sql
    for state in (
        "ready",
        "leased",
        "blocked",
        "succeeded",
        "dead",
        "cancelled",
        "quarantine_pending",
        "quarantined",
        "purge_pending",
    ):
        assert f"'{state}'" in task_sql


def test_coordinator_v3_fresh_schema_has_complete_invariant(tmp_path: Path) -> None:
    path = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"

    initialized = markdown_transaction.initialize_coordinator_v3_candidate(
        path, source_v2=None
    )
    validated = markdown_transaction.validate_coordinator_v3_database(
        path, state_root=tmp_path
    )

    assert initialized == {
        "operations": 0,
        "project_checkpoint_attempts": 0,
        "project_checkpoints": 0,
        "transactions": 0,
        "writer_fences": 0,
    }
    assert validated["application_id"] == COORDINATOR_APPLICATION_ID
    assert validated["user_version"] == 3
    assert validated["integrity_check"] == "ok"
    assert validated["foreign_key_check"] == []
    assert validated["journal_mode"] == "delete"
    assert validated["synchronous"] == 2
    assert validated["trusted_schema"] == 0
    with contextlib.closing(sqlite3.connect(path)) as database:
        objects = database.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {name for kind, name in objects if kind == "table"}
        triggers = {name for kind, name in objects if kind == "trigger"}
        transaction_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='transaction'"
        ).fetchone()[0]
        project_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='project_leases'"
        ).fetchone()[0]
        writer_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='writer_owners'"
        ).fetchone()[0]

    assert tables == COORDINATOR_V3_TABLES
    assert triggers == set()
    assert "'aborting'" in transaction_sql and "'aborted'" in transaction_sql
    for sql in (project_sql, writer_sql):
        assert "length(CAST(canonical_role AS BLOB)) BETWEEN 1 AND 64" in sql
        assert "length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512" in sql
        assert "length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256" in sql
        assert "process_id > 0" in sql
        assert "length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512" in sql
        assert "REFERENCES maintenance_owners" in sql


def test_coordinator_candidate_header_is_unpublished_until_schema_is_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"

    def interrupt(event: str) -> None:
        if event == "after_commit:create_transaction":
            raise RuntimeError("stop before complete coordinator schema")

    with pytest.raises(RuntimeError, match="complete coordinator schema"):
        markdown_transaction.initialize_coordinator_v3_candidate(
            path, source_v2=None, killpoint=interrupt
        )

    with contextlib.closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0
        assert database.execute("PRAGMA user_version").fetchone()[0] == 0

    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    with contextlib.closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == (
            COORDINATOR_APPLICATION_ID
        )
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3


def test_coordinator_candidate_resume_skips_completed_schema_statements(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"

    def interrupt(event: str) -> None:
        if event == "after_commit:create_transaction":
            raise RuntimeError("committed schema interruption")

    with pytest.raises(RuntimeError, match="committed schema interruption"):
        markdown_transaction.initialize_coordinator_v3_candidate(
            path, source_v2=None, killpoint=interrupt
        )

    events: list[str] = []
    markdown_transaction.initialize_coordinator_v3_candidate(
        path, source_v2=None, killpoint=events.append
    )

    assert "before:create_transaction" not in events
    assert events[0] == "before:create_operation"
    assert markdown_transaction.validate_coordinator_v3_database(
        path, state_root=tmp_path
    )["integrity_check"] == "ok"


def test_coordinator_candidate_initializer_never_calls_executescript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoExecuteScriptConnection(sqlite3.Connection):
        def executescript(self, sql_script: str, /):
            del sql_script
            pytest.fail("coordinator v3 initialization must execute one statement at a time")

    real_connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        kwargs["factory"] = NoExecuteScriptConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    path = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"

    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)

    assert markdown_transaction.validate_coordinator_v3_database(
        path, state_root=tmp_path
    )["integrity_check"] == "ok"


def test_queue_candidate_uses_fixed_application_id_and_user_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)

    with contextlib.closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0x4C575133
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3


def test_queue_candidate_header_is_unpublished_until_schema_is_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    def interrupt(event: str) -> None:
        if event == "after_commit:create_tasks":
            raise RuntimeError("stop before complete schema")

    with pytest.raises(RuntimeError, match="complete schema"):
        memory_queue.initialize_queue_v3_candidate(
            path,
            source_v2=None,
            killpoint=interrupt,
        )

    with contextlib.closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0
        assert database.execute("PRAGMA user_version").fetchone()[0] == 0

    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    assert memory_queue.validate_queue_v3_database(path, state_root=tmp_path)[
        "application_id"
    ] == 0x4C575133


@pytest.mark.parametrize("partial", ["tasks", "claim_index"])
def test_queue_partial_table_or_index_is_repaired(
    tmp_path: Path, partial: str
) -> None:
    path = tmp_path / "run" / f"partial-{partial}.sqlite3"
    if partial == "tasks":
        with contextlib.closing(
            open_operational_db(
                path,
                busy_ms=100,
                contract=QUEUE_CONTRACT,
                initialize_contract=True,
            )
        ) as database:
            database.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
    else:
        memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
        with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as database:
            database.execute("DROP INDEX queue_claim_order")

    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)

    result = memory_queue.validate_queue_v3_database(path, state_root=tmp_path)
    assert result["row_counts"]["tasks"] == 0


def test_queue_populated_partial_candidate_rebuilds_from_explicit_v2_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_queue = memory_queue.MemoryQueue(source_root)
    task_id = source_queue.enqueue("query", 1, {"prompt": "authoritative"})
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    with contextlib.closing(
        open_operational_db(
            path,
            busy_ms=100,
            contract=QUEUE_CONTRACT,
            initialize_contract=True,
        )
    ) as database:
        database.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        database.execute("INSERT INTO tasks(id) VALUES ('stale-partial')")

    summary = memory_queue.initialize_queue_v3_candidate(
        path, source_v2=source_queue.db_path
    )

    with contextlib.closing(sqlite3.connect(path)) as database:
        rows = database.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    assert summary["tasks"] == 1
    assert rows == [(task_id,)]


def test_queue_v2_backup_rebuild_drops_dependent_indexes_before_tables(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_queue = memory_queue.MemoryQueue(source_root)
    task_id = source_queue.enqueue("query", 1, {"prompt": "backed-up"})
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    candidate.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(source_queue.db_path)) as source:
        with contextlib.closing(sqlite3.connect(candidate)) as target:
            source.backup(target)

    summary = memory_queue.initialize_queue_v3_candidate(
        candidate, source_v2=source_queue.db_path
    )

    assert summary["tasks"] == 1
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        assert database.execute("SELECT id FROM tasks").fetchall() == [(task_id,)]


def test_queue_candidate_initializer_never_calls_executescript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoExecuteScriptConnection(sqlite3.Connection):
        def executescript(self, sql_script: str, /):
            del sql_script
            pytest.fail("queue v3 initialization must execute one statement at a time")

    real_connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        kwargs["factory"] = NoExecuteScriptConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)

    assert memory_queue.validate_queue_v3_database(path, state_root=tmp_path)[
        "integrity_check"
    ] == "ok"


def test_queue_v3_validator_rejects_nonterminal_payload_hash_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    now = "2026-08-05T12:00:00+00:00"
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_blob, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES ('tampered', 'query', 1, ?, ?, 'ready', 0, ?, ?, ?)""",
            (b"{}", "0" * 64, now, now, now),
        )
        database.commit()

    with pytest.raises(OperationalDatabaseContractError) as raised:
        memory_queue.validate_queue_v3_database(path, state_root=tmp_path)

    assert raised.value.code == "queue_v3_validation_failed"


@pytest.mark.parametrize("payload", [b"not-json", b'{"b":1, "a":2}'])
def test_queue_v3_validator_rejects_hash_valid_noncanonical_payload(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    now = "2026-08-05T12:00:00+00:00"
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_blob, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES ('malformed', 'query', 1, ?, ?, 'ready', 0, ?, ?, ?)""",
            (payload, memory_queue.sha256_bytes(payload), now, now, now),
        )
        database.commit()

    with pytest.raises(OperationalDatabaseContractError) as raised:
        memory_queue.validate_queue_v3_database(path, state_root=tmp_path)

    assert raised.value.code == "queue_v3_validation_failed"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
@pytest.mark.parametrize("database", ["queue", "coordinator"])
def test_v3_candidate_rejects_same_file_identity_as_v2_source(
    tmp_path: Path,
    database: str,
) -> None:
    if database == "queue":
        source = memory_queue.MemoryQueue(tmp_path / "source").db_path
        initializer = memory_queue.initialize_queue_v3_candidate
    else:
        vault = tmp_path / "vault"
        (vault / "knowledge").mkdir(parents=True)
        source = markdown_transaction.MarkdownCoordinator(
            vault, tmp_path / "source"
        ).database_path
        initializer = markdown_transaction.initialize_coordinator_v3_candidate
    candidate = source.with_name(f"{database}-hardlink-candidate.sqlite3")
    try:
        os.link(source, candidate)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    source_bytes = source.read_bytes()

    with pytest.raises(ValueError, match="same file"):
        initializer(candidate, source_v2=source)

    assert source.read_bytes() == source_bytes


@pytest.mark.parametrize("database", ["queue", "coordinator"])
def test_v2_migration_rejects_unknown_schema_objects(
    tmp_path: Path,
    database: str,
) -> None:
    if database == "queue":
        source = memory_queue.MemoryQueue(tmp_path / "source").db_path
        initializer = memory_queue.initialize_queue_v3_candidate
    else:
        vault = tmp_path / "vault"
        (vault / "knowledge").mkdir(parents=True)
        source = markdown_transaction.MarkdownCoordinator(
            vault, tmp_path / "source"
        ).database_path
        initializer = markdown_transaction.initialize_coordinator_v3_candidate
    with contextlib.closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE future_evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_evidence VALUES ('retained')")
        connection.commit()
    candidate = tmp_path / "run" / f"{database}-v3.candidate.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        initializer(candidate, source_v2=source)

    assert raised.value.code in {
        "queue_v2_schema_unknown",
        "coordinator_v2_schema_unknown",
    }


def test_v3_connection_reads_back_delete_full_foreign_keys_and_untrusted_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    with contextlib.closing(
        open_operational_db(
            path,
            busy_ms=321,
            contract=QUEUE_CONTRACT,
            initialize_contract=True,
        )
    ) as database:
        assert database.isolation_level is None
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert database.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert database.execute("PRAGMA busy_timeout").fetchone()[0] == 321
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0x4C575133
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3

    with contextlib.closing(
        open_operational_db(path, busy_ms=321, contract=QUEUE_CONTRACT)
    ) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0x4C575133


@pytest.mark.parametrize(
    ("pragma", "value"),
    [("application_id", 123), ("user_version", 2)],
)
def test_v3_connection_rejects_wrong_application_id_or_user_version(
    tmp_path: Path,
    pragma: str,
    value: int,
) -> None:
    path = tmp_path / "run" / f"wrong-{pragma}.sqlite3"
    with contextlib.closing(
        open_operational_db(
            path,
            busy_ms=100,
            contract=QUEUE_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute(f"PRAGMA {pragma}={value}")

    with pytest.raises(OperationalDatabaseContractError) as raised:
        open_operational_db(path, busy_ms=100, contract=QUEUE_CONTRACT)

    assert raised.value.code == "operational_database_contract_mismatch"


def test_contract_initialization_rejects_conflicting_existing_header(tmp_path: Path) -> None:
    path = tmp_path / "run" / "conflict.sqlite3"
    path.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute("PRAGMA application_id=99")

    with pytest.raises(OperationalDatabaseContractError):
        open_operational_db(
            path,
            busy_ms=100,
            contract=QUEUE_CONTRACT,
            initialize_contract=True,
        )


def test_each_migration_statement_has_before_after_execute_and_after_commit_killpoints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "events.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        database.execute("CREATE TABLE migration_values(name TEXT PRIMARY KEY)")
        statements = tuple(
            MigrationStatement(
                name=f"insert_{name}",
                sql="INSERT INTO migration_values(name) VALUES (?)",
                parameters=(name,),
                completed=lambda current, expected=name: _row_exists(current, expected),
            )
            for name in ("first", "second")
        )
        events: list[str] = []

        run_resumable_migration(
            database,
            statements,
            final_invariant=lambda current: all(
                _row_exists(current, name) for name in ("first", "second")
            ),
            killpoint=events.append,
        )

    assert events == [
        "before:insert_first",
        "after_execute:insert_first",
        "after_commit:insert_first",
        "before:insert_second",
        "after_execute:insert_second",
        "after_commit:insert_second",
    ]


def test_restart_skips_only_a_statement_with_its_completed_invariant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "restart.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        database.execute("CREATE TABLE migration_values(name TEXT PRIMARY KEY)")
        statements = tuple(
            MigrationStatement(
                name=f"insert_{name}",
                sql="INSERT INTO migration_values(name) VALUES (?)",
                parameters=(name,),
                completed=lambda current, expected=name: _row_exists(current, expected),
            )
            for name in ("first", "second")
        )

        def crash_after_first(event: str) -> None:
            if event == "after_commit:insert_first":
                raise RuntimeError("simulated committed crash")

        with pytest.raises(RuntimeError, match="committed crash"):
            run_resumable_migration(
                database,
                statements,
                final_invariant=lambda _current: False,
                killpoint=crash_after_first,
            )
        assert _row_exists(database, "first")
        assert not _row_exists(database, "second")

        resumed_events: list[str] = []
        run_resumable_migration(
            database,
            statements,
            final_invariant=lambda current: all(
                _row_exists(current, name) for name in ("first", "second")
            ),
            killpoint=resumed_events.append,
        )

    assert resumed_events == [
        "before:insert_second",
        "after_execute:insert_second",
        "after_commit:insert_second",
    ]


def test_after_execute_failure_rolls_back_and_restart_replays_statement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "rollback.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        database.execute("CREATE TABLE migration_values(name TEXT PRIMARY KEY)")
        statement = MigrationStatement(
            name="insert_value",
            sql="INSERT INTO migration_values(name) VALUES ('value')",
            completed=lambda current: _row_exists(current, "value"),
        )

        def crash_before_commit(event: str) -> None:
            if event == "after_execute:insert_value":
                raise RuntimeError("simulated pre-commit crash")

        with pytest.raises(RuntimeError, match="pre-commit crash"):
            run_resumable_migration(
                database,
                (statement,),
                final_invariant=lambda _current: False,
                killpoint=crash_before_commit,
            )
        assert not _row_exists(database, "value")

        run_resumable_migration(
            database,
            (statement,),
            final_invariant=lambda current: _row_exists(current, "value"),
        )
        assert _row_exists(database, "value")


def test_column_presence_without_backfill_index_or_marker_is_incomplete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "complete-invariant.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        database.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO records(id, value) VALUES (1, NULL)")
        database.execute("CREATE TABLE migration_values(name TEXT PRIMARY KEY)")

        def backfilled(current: sqlite3.Connection) -> bool:
            return current.execute(
                "SELECT COUNT(*) FROM records WHERE value IS NULL"
            ).fetchone()[0] == 0

        def indexed(current: sqlite3.Connection) -> bool:
            return current.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'index' AND name = 'records_value'"
            ).fetchone() is not None

        statements = (
            MigrationStatement(
                "backfill_value",
                "UPDATE records SET value = 'migrated' WHERE value IS NULL",
                completed=backfilled,
            ),
            MigrationStatement(
                "create_index",
                "CREATE INDEX records_value ON records(value)",
                completed=indexed,
            ),
            MigrationStatement(
                "record_marker",
                "INSERT INTO migration_values(name) VALUES ('schema-v3')",
                completed=lambda current: _row_exists(current, "schema-v3"),
            ),
        )

        run_resumable_migration(
            database,
            statements,
            final_invariant=lambda current: (
                backfilled(current) and indexed(current) and _row_exists(current, "schema-v3")
            ),
        )

        assert database.execute("SELECT value FROM records WHERE id = 1").fetchone()[0] == (
            "migrated"
        )


def test_statement_and_final_invariants_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "run" / "invariant.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        with pytest.raises(OperationalDatabaseContractError) as statement_error:
            run_resumable_migration(
                database,
                (
                    MigrationStatement(
                        "create_sample",
                        "CREATE TABLE sample(value INTEGER)",
                        completed=lambda _current: False,
                    ),
                ),
                final_invariant=lambda _current: False,
            )
        assert statement_error.value.code == "operational_migration_incomplete"
        assert not _table_exists(database, "sample")

        with pytest.raises(OperationalDatabaseContractError) as final_error:
            run_resumable_migration(
                database,
                (),
                final_invariant=lambda _current: False,
            )
        assert final_error.value.code == "operational_migration_incomplete"


def test_migration_statement_names_are_unique_and_nonempty(tmp_path: Path) -> None:
    path = tmp_path / "run" / "names.sqlite3"
    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        statement = MigrationStatement("same", "SELECT 1", completed=lambda _current: True)
        with pytest.raises(ValueError, match="unique"):
            run_resumable_migration(
                database,
                (statement, statement),
                final_invariant=lambda _current: True,
            )
        with pytest.raises(ValueError, match="name"):
            run_resumable_migration(
                database,
                (MigrationStatement("", "SELECT 1", completed=lambda _current: True),),
                final_invariant=lambda _current: True,
            )


@pytest.mark.parametrize(
    "event",
    ["before:create_values", "after_execute:create_values", "after_commit:create_values"],
)
def test_subprocess_crash_at_every_statement_boundary_resumes_exactly(
    tmp_path: Path,
    event: str,
) -> None:
    path = tmp_path / "run" / f"subprocess-{event.split(':')[0]}.sqlite3"
    script = """
import contextlib
import os
import sys
from pathlib import Path

from reliable_memory import MigrationStatement, open_operational_db, run_resumable_migration

path = Path(sys.argv[1])
target = sys.argv[2]

def completed(database):
    return database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'values_table'"
    ).fetchone() is not None

with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
    run_resumable_migration(
        database,
        (MigrationStatement("create_values", "CREATE TABLE values_table(value INTEGER)", completed),),
        final_invariant=completed,
        killpoint=lambda current: os._exit(86) if current == target else None,
    )
"""
    environment = os.environ.copy()
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    environment["PYTHONPATH"] = scripts + os.pathsep + environment.get("PYTHONPATH", "")
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(path), event],
        check=False,
        env=environment,
    )
    assert crashed.returncode == 86

    with contextlib.closing(open_operational_db(path, busy_ms=100)) as database:
        events: list[str] = []

        def completed(current: sqlite3.Connection) -> bool:
            return _table_exists(current, "values_table")

        run_resumable_migration(
            database,
            (MigrationStatement("create_values", "CREATE TABLE values_table(value INTEGER)", completed),),
            final_invariant=completed,
            killpoint=events.append,
        )
        assert _table_exists(database, "values_table")

    if event == "after_commit:create_values":
        assert events == []
    else:
        assert events == [
            "before:create_values",
            "after_execute:create_values",
            "after_commit:create_values",
        ]
