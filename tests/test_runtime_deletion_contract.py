from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _transaction_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE "transaction" (
                id TEXT PRIMARY KEY, operation_id TEXT, request_hash TEXT,
                state TEXT, preconditions_json TEXT, plan_hash TEXT,
                created_at TEXT, updated_at TEXT,
                artifacts_pruned_at TEXT
            );
            CREATE TABLE "operation" (
                transaction_id TEXT, position INTEGER, kind TEXT, path TEXT,
                before_hash TEXT, after_hash TEXT, parent_device INTEGER,
                parent_inode INTEGER, applied INTEGER
            );
            CREATE TABLE project_leases (
                project TEXT PRIMARY KEY, expires_at TEXT
            );
            CREATE TABLE writer_owners (
                gate_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            CREATE TABLE maintenance_owners (
                owner_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            """
        )
        connection.executemany(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (name, f"operation-{name}", "a" * 64, state, "{}", "b" * 64,
                 now.isoformat(), now.isoformat(), None)
                for name, state in (
                    ("tx-active", "applying"),
                    ("tx-conflict", "conflicted"),
                    ("tx-quarantine", "quarantined"),
                    ("tx-undo", "committed"),
                )
            ],
        )
        connection.executemany(
            'INSERT INTO "operation" VALUES (?, 0, "create", ?, "absent", ?, 1, 2, 1)',
            [
                (name, f"knowledge/notes/{name}.md", "c" * 64)
                for name in ("tx-active", "tx-conflict", "tx-quarantine", "tx-undo")
            ],
        )
        future = (now + timedelta(minutes=5)).isoformat()
        connection.execute("INSERT INTO project_leases VALUES ('p', ?)", (future,))
        connection.execute("INSERT INTO writer_owners VALUES ('global', 1, ?)", (future,))
        connection.execute("INSERT INTO maintenance_owners VALUES ('doctor', 1, ?)", (future,))
    for name in ("tx-active", "tx-conflict", "tx-quarantine", "tx-undo"):
        (state_root / "run/transactions" / name).mkdir(parents=True, exist_ok=True)
    return database


def _queue_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, state TEXT, error_code TEXT,
                blocked_capability TEXT, result_reference TEXT
            );
            CREATE TABLE queue_ownership (
                role TEXT PRIMARY KEY, token TEXT, pid INTEGER, expires_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES "
            "('task', 'dead', 'attempts_exhausted', NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO queue_ownership VALUES ('worker', 'token', 1, ?)",
            ((now + timedelta(minutes=5)).isoformat(),),
        )
    results = state_root / "run/queue-results"
    results.mkdir()
    (results / "orphan.result").write_text("retained", encoding="utf-8")
    return database


def test_run_deletion_reports_every_contract_blocker(tmp_path, monkeypatch):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _transaction_db(state_root, now)
    _queue_db(state_root, now)
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    result = doctor._run_deletion_check(state_root, now)

    assert {item["code"] for item in result["blockers"]} == {
        "transaction_nonterminal",
        "transaction_conflicted",
        "transaction_quarantined",
        "transaction_undo_retained",
        "queue_task_retained",
        "queue_result_retained",
        "project_lease_live",
        "writer_live",
        "queue_worker_live",
        "maintenance_owner_live",
    }
    assert result["allowed"] is False
    assert "tx-active" not in json.dumps(result)


def test_run_deletion_is_allowed_only_when_no_blocker_exists(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)

    assert doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc)
    ) == {"allowed": True, "blockers": []}


def test_installers_and_doctor_never_remove_run_source_contract():
    root = Path(__file__).resolve().parent.parent
    sources = [
        (root / "install.sh").read_text(encoding="utf-8"),
        (root / "install.ps1").read_text(encoding="utf-8"),
        (root / "scripts/doctor.py").read_text(encoding="utf-8"),
    ]
    forbidden = ("rm -rf $STATE_ROOT/run", "Remove-Item $STATE_ROOT\\run", "rmtree(state_root / \"run\")")
    assert all(token not in source for source in sources for token in forbidden)


def test_deletion_blocks_every_legacy_and_retained_queue_artifact(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    run = state_root / "run"
    legacy = run / "queue"
    results = run / "queue-results"
    quarantine = run / "queue-quarantine"
    for directory in (legacy, results, quarantine):
        directory.mkdir(parents=True, exist_ok=True)
    (legacy / "pending.json").write_text("{}", encoding="utf-8")
    (legacy / "leased.processing").write_text("broken", encoding="utf-8")
    (results / "retained.tmp").write_text("result", encoding="utf-8")
    (quarantine / "bad.json").write_text("{}", encoding="utf-8")

    result = doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc), deadline=float("inf")
    )

    assert {item["code"] for item in result["blockers"]} >= {
        "legacy_queue_retained",
        "legacy_queue_malformed",
        "queue_result_retained",
        "queue_quarantine_retained",
    }


def test_deletion_blocks_source_state_and_any_partial_database_error(
    tmp_path, monkeypatch
):
    import doctor

    state_root = tmp_path / "state"
    path = state_root / "run/queue.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, state TEXT, error_code TEXT,
                blocked_capability TEXT
            );
            INSERT INTO tasks VALUES('task','ready',NULL,NULL);
            CREATE TABLE source_fences(daily_id TEXT);
            INSERT INTO source_fences VALUES('2026-01-01');
            CREATE TABLE source_failures(logical_path TEXT);
            INSERT INTO source_failures VALUES('knowledge/daily/2026-01-01.md');
            """
        )
    real = doctor._readonly_database

    class BrokenAfterRows:
        def __enter__(self):
            self.database = real(path, state_root, max_bytes=doctor.MAX_OPERATIONAL_DB_BYTES)
            return self

        def __exit__(self, *args):
            self.database.close()

        def execute(self, sql, parameters=()):
            if "source_failures" in sql:
                raise sqlite3.DatabaseError("corrupt tail")
            return self.database.execute(sql, parameters)

    monkeypatch.setattr(doctor, "_readonly_database", lambda *args, **kwargs: BrokenAfterRows())

    queue = doctor._queue_v2_check(
        state_root, datetime.now(timezone.utc), float("inf")
    )
    deletion = doctor._run_deletion_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"queue": queue},
    )

    assert queue["details"]["read_error"] is True
    assert "queue_state_unreadable" in {
        item["code"] for item in deletion["blockers"]
    }


def test_deletion_reuses_collected_checks_without_rescanning(tmp_path, monkeypatch):
    import doctor

    transaction = {
        "id": "transactions",
        "status": "ok",
        "details": {"deletion_codes": []},
    }
    queue = {"id": "queue", "status": "ok", "details": {"deletion_codes": []}}
    archive = {"id": "archives", "status": "ok", "details": {"deletion_codes": []}}
    monkeypatch.setattr(
        doctor,
        "_transaction_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )
    monkeypatch.setattr(
        doctor,
        "_queue_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )

    result = doctor._run_deletion_check(
        tmp_path,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"transactions": transaction, "queue": queue, "archives": archive},
    )

    assert result == {"allowed": True, "blockers": []}


def _malformed_transaction_db(
    state_root: Path,
    now: datetime,
    *,
    state: object = "committed",
    updated_at: object | None = None,
    created_at: object = "valid",
    request_hash: object = "a" * 64,
    plan_hash: object = "b" * 64,
    operation_transaction_id: str = "tx-health",
    operation_kind: str = "create",
    before_hash: object = "absent",
    after_hash: object = "c" * 64,
    create_artifact: bool = True,
) -> None:
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    timestamp = now.isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE "transaction" (
                id, operation_id, request_hash, state, preconditions_json,
                plan_hash, created_at, updated_at, artifacts_pruned_at
            );
            CREATE TABLE "operation" (
                transaction_id, position, kind, path, before_hash, after_hash,
                parent_device, parent_inode, applied
            );
            """
        )
        connection.execute(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
            (
                "tx-health",
                "operation-health",
                request_hash,
                state,
                "{}",
                plan_hash,
                timestamp if created_at == "valid" else created_at,
                timestamp if updated_at is None else updated_at,
            ),
        )
        connection.execute(
            'INSERT INTO "operation" VALUES (?, 0, ?, ?, ?, ?, 1, 2, 1)',
            (
                operation_transaction_id,
                operation_kind,
                "knowledge/notes/health.md",
                before_hash,
                after_hash,
            ),
        )
    if create_artifact:
        (state_root / "run/transactions/tx-health").mkdir(parents=True)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"state": "invented"}, "transaction_state_unknown"),
        ({"updated_at": "not-a-date"}, "transaction_state_corrupt"),
        ({"created_at": None}, "transaction_state_corrupt"),
        ({"request_hash": None}, "transaction_state_corrupt"),
        ({"request_hash": "short"}, "transaction_state_corrupt"),
        ({"plan_hash": "short"}, "transaction_state_corrupt"),
        (
            {"before_hash": "c" * 64, "after_hash": "absent"},
            "transaction_state_corrupt",
        ),
        ({"create_artifact": False}, "transaction_state_corrupt"),
        (
            {"operation_transaction_id": "missing-transaction"},
            "transaction_state_corrupt",
        ),
    ],
)
def test_transaction_health_blocks_unknown_or_corrupt_rows(
    tmp_path, mutation, expected_code
):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _malformed_transaction_db(state_root, now, **mutation)

    check = doctor._transaction_check(state_root, now)
    deletion = doctor._run_deletion_check(
        state_root, now, collected={"transactions": check}
    )

    assert check["status"] == "error"
    assert expected_code in check["details"]["deletion_codes"]
    assert deletion["allowed"] is False


def test_transaction_health_blocks_missing_required_schema(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE "transaction" (id, state, updated_at)'
        )

    check = doctor._transaction_check(
        state_root, datetime.now(timezone.utc)
    )

    assert check["status"] == "error"
    assert "transaction_state_corrupt" in check["details"]["deletion_codes"]


def test_recent_committed_transaction_with_malformed_date_cannot_allow_deletion(
    tmp_path,
):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _malformed_transaction_db(state_root, now, updated_at="recent-but-malformed")

    result = doctor._run_deletion_check(state_root, now)

    assert result["allowed"] is False
    assert "transaction_state_corrupt" in {
        item["code"] for item in result["blockers"]
    }


def _retained_queue_db(state_root: Path, now: datetime) -> None:
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    results = state_root / "run/queue-results"
    results.mkdir(parents=True)
    result = results / "done.json"
    result.write_bytes(b'{"ok":true}')
    result.chmod(0o600)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, state, error_code, blocked_capability, "
            "lease_expires_at, result_reference, result_sha256)"
        )
        connection.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            [
                (
                    "done",
                    "succeeded",
                    None,
                    None,
                    None,
                ),
                ("cancelled", "cancelled", "cancelled", None, None),
                ("dead", "dead", "attempts_exhausted", None, None),
            ],
        )
    (state_root / "run/queue-migrated-v2").write_text("complete", encoding="utf-8")


def test_policy_retention_blocks_deletion_without_degrading_health(tmp_path, monkeypatch):
    import doctor
    import session_start_context

    from tests.test_doctor import _build_root, _create_claim_index, _create_index

    root, state_root, home = _build_root(tmp_path)
    now = datetime.now(timezone.utc)
    _malformed_transaction_db(state_root, now)
    _retained_queue_db(state_root, now)
    (state_root / "run/state.json").write_text(
        json.dumps(
            {
                "last_nightly_date": now.date().isoformat(),
                "last_nightly_status": "success",
            }
        ),
        encoding="utf-8",
    )
    index = state_root / "cache/index.sqlite"
    _create_index(index)
    _create_claim_index(root, state_root)
    os.utime(index, (now.timestamp(), now.timestamp()))

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, now=now)
    checks = {check["id"]: check for check in report["checks"]}

    assert report["run_deletion"]["allowed"] is False
    assert {
        "transaction_undo_retained",
        "queue_task_retained",
        "queue_result_retained",
    }.issubset(
        {item["code"] for item in report["run_deletion"]["blockers"]}
    )
    assert checks["transactions"]["status"] == "ok"
    assert checks["queue"]["status"] == "ok", checks["queue"]
    assert checks["run_deletion"]["status"] == "ok"
    assert report["overall_status"] == "ok"
    assert doctor.degraded_summary(report) == ""
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: report)
    assert session_start_context.health_block() == ""


@pytest.mark.parametrize(
    ("state", "error_code"),
    [
        ("invented", None),
        ("succeeded", "unexpected_failure"),
        ("dead", None),
        ("cancelled", "wrong_code"),
    ],
)
def test_queue_health_fails_closed_on_unknown_state_or_error_metadata(
    tmp_path, state, error_code
):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, state, error_code, blocked_capability, "
            "lease_expires_at, result_reference, result_sha256)"
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('task', ?, ?, NULL, NULL, NULL, NULL)",
            (state, error_code),
        )
    (state_root / "run/queue-migrated-v2").write_text("complete", encoding="utf-8")

    check = doctor._queue_v2_check(state_root, datetime.now(timezone.utc), float("inf"))
    deletion = doctor._run_deletion_check(
        state_root,
        datetime.now(timezone.utc),
        collected={"queue": check},
    )

    assert check["status"] == "error"
    assert {
        "queue_state_unknown",
        "queue_state_corrupt",
    } & set(check["details"]["deletion_codes"])
    assert deletion["allowed"] is False


def test_queue_health_fails_closed_when_required_state_metadata_is_missing(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, error_code, blocked_capability)"
        )
        connection.execute("INSERT INTO tasks VALUES ('task', NULL, NULL)")

    check = doctor._queue_v2_check(
        state_root, datetime.now(timezone.utc), float("inf")
    )

    assert check["status"] == "error"
    assert "queue_state_corrupt" in check["details"]["deletion_codes"]
